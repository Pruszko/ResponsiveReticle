from math import pi
from typing import Optional

import BattleReplay
import BigWorld
import Math
import constants
from Avatar import PlayerAvatar
from AvatarInputHandler import AimingSystems
from AvatarInputHandler.control_modes import _GunControlMode
from VehicleGunRotator import VehicleGunRotator
from aih_constants import GUN_MARKER_TYPE
from gun_rotation_shared import calcPitchLimitsFromDesc, calcGunPitchCorrection
from projectile_trajectory import getShotAngles

from responsive_reticle.helpers.one_tick_cache import g_oneTickCache, old_VGR_getAvatarOwnVehicleStabilisedMatrix
from responsive_reticle.utils import overrideIn, isClientWG, isClientLesta


# __ROTATION_TICK_LENGTH controls, how fast gun rotator updates vehicle turret rotation and gun markers
#
# __INSUFFICIENT_TIME_DIFF controls how small the time diff must be before rotation update can be performed
# because by default it is 0.02 (where rotation __ROTATION_TICK_LENGTH was 0.1), we have to lower it some reasonably
# to allow faster tick-rate


old__onTick = VehicleGunRotator._VehicleGunRotator__onTick


@overrideIn(VehicleGunRotator)
def __onTick(self):
    try:
        g_oneTickCache.isDuringVgrTick = True
        g_oneTickCache.updateCache()

        if g_oneTickCache.shouldBoostTickRate:
            # contract of BigWorld.callback(delay, func) which is used with those constants, is
            # that BigWorld is required to call func no earlier than provided delay
            # but that doesn't mean it will be exactly this delay - it might be delayed even more
            #
            # we set callback to 0.001 because we want gun rotator to be called in next game tick,
            # but we DO NOT accept zero delay
            # otherwise, in gun rotator code there would be division by zero in some places
            # and overall we don't want it to be this close to floating-point precision limits
            VehicleGunRotator._VehicleGunRotator__ROTATION_TICK_LENGTH = 0.001
            VehicleGunRotator._VehicleGunRotator__INSUFFICIENT_TIME_DIFF = 0.0005
        else:
            VehicleGunRotator._VehicleGunRotator__ROTATION_TICK_LENGTH = constants.SERVER_TICK_LENGTH
            VehicleGunRotator._VehicleGunRotator__INSUFFICIENT_TIME_DIFF = 0.02

        old__onTick(self)
    finally:
        g_oneTickCache.isDuringVgrTick = False


# Avatar.getOwnVehicleShotDispersionAngle() is not only a "getter"
# it does modify player state related to reticle size
#
# the problem is:
# - when, with high tick-rate, we move reticle even slightly, it is registered as "full turret move"
# due to very low time diff in which this move happened (low maximum turret yaw angle) - resulting in big reticle bloom
# which doesn't actually happen on the server (and vanilla client)
# - for the same small mouse movement this does not happen on lower tick-rate,
# because time diff is big (0.1 sec), so proportionally maximum turret yaw angle is very big,
# so it won't be "full turret move" but just very small turret move, which is registered as much smaller reticle bloom
#
# we have to somehow compensate for that
# the problem is (again) - position updates are tied to dispersion angle updates,
# and we have to somehow separate them now
#
# AND - we have to interpolate that size
# otherwise it would be very stuttering


# we unfortunately have to override entire method,
# because only in this place we want to capture avatar.getOwnVehicleShotDispersionAngle() calls
#
# we want to alter them, because we have to separate invocation rate of that method from gun rotator code
# which we would do by introducing cache for 0.1 second
# that return last computed values (and calls that method only when computing it)
# and do it in clever way to simulate slower tick-rate
#
# performance note
#
# this method takes around 230 us on my machine and does the main rotation math
# with modded logic - around 75 us - very nice!
#
# also WG and Lesta specific
# slightly different method body

@overrideIn(VehicleGunRotator, condition=isClientWG)
def __rotate(self, shotPoint, timeDiff):
    self._VehicleGunRotator__turretRotationSpeed = 0.0
    targetPoint = shotPoint if shotPoint is not None else self._VehicleGunRotator__prevSentShotPoint

    # performance note
    #
    # avoid checking replayCtrl.isUpdateGunOnTimeWarp - we're 100% outside replay
    #
    # replayCtrl = BattleReplay.g_replayCtrl
    # if targetPoint is None or self._VehicleGunRotator__isLocked and not replayCtrl.isUpdateGunOnTimeWarp:
    if targetPoint is None or self._VehicleGunRotator__isLocked:
        if g_oneTickCache.shouldBoostTickRate:
            self._VehicleGunRotator__dispersionAngles = getOwnVehicleShotDispersionAngleForGunRotator(self)
        else:
            self._VehicleGunRotator__dispersionAngles = self._avatar.getOwnVehicleShotDispersionAngle(0.0)
    else:
        avatar = self._avatar
        descr = avatar.getVehicleDescriptor()
        turretYawLimits = self._VehicleGunRotator__getTurretYawLimits()
        maxTurretRotationSpeed = self._VehicleGunRotator__maxTurretRotationSpeed
        prevTurretYaw = self._VehicleGunRotator__turretYaw
        vehicleMatrix = self.getAvatarOwnVehicleStabilisedMatrix()
        if self._VehicleGunRotator__fixedShotAngles is not None:
            shotTurretYaw, shotGunPitch = self._VehicleGunRotator__fixedShotAngles
        else:
            shotTurretYaw, shotGunPitch = getShotAngles(descr, vehicleMatrix, targetPoint,
                                                        overrideGunPosition=self._VehicleGunRotator__gunPosition)
        estimatedTurretYaw = self.getNextTurretYaw(prevTurretYaw, shotTurretYaw, maxTurretRotationSpeed * timeDiff,
                                                   turretYawLimits)
        # performance note
        #
        # avoid checking replayCtrl.isPlaying - we're 100% outside replay
        #
        # if not replayCtrl.isPlaying:
        #     self._VehicleGunRotator__turretYaw = turretYaw = self._VehicleGunRotator__syncWithServerTurretYaw(estimatedTurretYaw)
        # else:
        #     self._VehicleGunRotator__turretYaw = turretYaw = estimatedTurretYaw
        self._VehicleGunRotator__turretYaw = turretYaw = self._VehicleGunRotator__syncWithServerTurretYaw(estimatedTurretYaw)

        if maxTurretRotationSpeed != 0:
            self.estimatedTurretRotationTime = abs(turretYaw - shotTurretYaw) / maxTurretRotationSpeed
        else:
            self.estimatedTurretRotationTime = 0
        gunPitchLimits = calcPitchLimitsFromDesc(turretYaw, self._VehicleGunRotator__getGunPitchLimits(),
                                                 descr.hull.turretPitches[0], descr.turret.gunJointPitch)
        self._VehicleGunRotator__gunPitch = self.getNextGunPitch(self._VehicleGunRotator__gunPitch, shotGunPitch, timeDiff, gunPitchLimits)

        # performance note
        #
        # avoid checking replayCtrl.isPlaying and replayCtrl.isUpdateGunOnTimeWarp - we're 100% outside replay
        # if replayCtrl.isPlaying and replayCtrl.isUpdateGunOnTimeWarp:
        #     self._VehicleGunRotator__updateTurretMatrix(turretYaw, 0.0)
        #     self._VehicleGunRotator__updateGunMatrix(self._VehicleGunRotator__gunPitch, 0.0)
        # else:
        #     self._VehicleGunRotator__updateTurretMatrix(turretYaw, self._VehicleGunRotator__ROTATION_TICK_LENGTH)
        #     self._VehicleGunRotator__updateGunMatrix(self._VehicleGunRotator__gunPitch, self._VehicleGunRotator__ROTATION_TICK_LENGTH)
        self._VehicleGunRotator__updateTurretMatrix(turretYaw, self._VehicleGunRotator__ROTATION_TICK_LENGTH)
        self._VehicleGunRotator__updateGunMatrix(self._VehicleGunRotator__gunPitch, self._VehicleGunRotator__ROTATION_TICK_LENGTH)

        diff = abs(estimatedTurretYaw - prevTurretYaw)
        if diff > pi:
            diff = 2 * pi - diff
        self._VehicleGunRotator__turretRotationSpeed = diff / timeDiff

        if g_oneTickCache.shouldBoostTickRate:
            self._VehicleGunRotator__dispersionAngles = getOwnVehicleShotDispersionAngleForGunRotator(self)
        else:
            self._VehicleGunRotator__dispersionAngles = avatar.getOwnVehicleShotDispersionAngle(self._VehicleGunRotator__turretRotationSpeed)


@overrideIn(VehicleGunRotator, condition=isClientLesta)
def __rotate(self, shotPoint, timeDiff):
    self._VehicleGunRotator__turretRotationSpeed = 0.0
    targetPoint = shotPoint if shotPoint is not None else self._VehicleGunRotator__prevSentShotPoint

    # performance note
    #
    # avoid checking replayCtrl.isUpdateGunOnTimeWarp - we're 100% outside replay
    #
    # replayCtrl = BattleReplay.g_replayCtrl
    # if targetPoint is None or self._VehicleGunRotator__isLocked and not replayCtrl.isUpdateGunOnTimeWarp:
    if targetPoint is None or self._VehicleGunRotator__isLocked:
        if g_oneTickCache.shouldBoostTickRate:
            self._VehicleGunRotator__dispersionAngles = getOwnVehicleShotDispersionAngleForGunRotator(self)
        else:
            self._VehicleGunRotator__dispersionAngles = self._avatar.getOwnVehicleShotDispersionAngle(0.0)
    else:
        avatar = self._avatar
        descr = avatar.getVehicleDescriptor()
        turretYawLimits = self._VehicleGunRotator__getTurretYawLimits()
        maxTurretRotationSpeed = self._VehicleGunRotator__maxTurretRotationSpeed
        prevTurretYaw = self._VehicleGunRotator__turretYaw
        vehicleMatrix = self.getAvatarOwnVehicleStabilisedMatrix()
        shotTurretYaw, shotGunPitch = getShotAngles(descr, vehicleMatrix, (
            prevTurretYaw, self._VehicleGunRotator__gunPitch), targetPoint, overrideGunPosition=self._VehicleGunRotator__gunPosition)
        estimatedTurretYaw = self.getNextTurretYaw(prevTurretYaw, shotTurretYaw, maxTurretRotationSpeed * timeDiff,
                                                   turretYawLimits)
        self._VehicleGunRotator__turretYaw = turretYaw = self._VehicleGunRotator__syncWithServerTurretYaw(estimatedTurretYaw)
        if maxTurretRotationSpeed != 0:
            self.estimatedTurretRotationTime = abs(turretYaw - shotTurretYaw) / maxTurretRotationSpeed
        else:
            self.estimatedTurretRotationTime = 0
        gunPitchLimits = calcPitchLimitsFromDesc(turretYaw, self._VehicleGunRotator__getGunPitchLimits(), descr.hull.turretPitches[0],
                                                 descr.turret.gunJointPitch)
        self._VehicleGunRotator__gunPitch = self.getNextGunPitch(self._VehicleGunRotator__gunPitch, shotGunPitch, timeDiff, gunPitchLimits)

        # performance note
        #
        # avoid checking replayCtrl.isPlaying - we're 100% outside replay
        #
        # if replayCtrl.isPlaying and replayCtrl.isUpdateGunOnTimeWarp:
        #     self._VehicleGunRotator__updateTurretMatrix(turretYaw, 0.0)
        #     self._VehicleGunRotator__updateGunMatrix(self._VehicleGunRotator__gunPitch, 0.0)
        # else:
        #     self._VehicleGunRotator__updateTurretMatrix(turretYaw, self._VehicleGunRotator__ROTATION_TICK_LENGTH)
        #     self._VehicleGunRotator__updateGunMatrix(self._VehicleGunRotator__gunPitch, self._VehicleGunRotator__ROTATION_TICK_LENGTH)
        self._VehicleGunRotator__updateTurretMatrix(turretYaw, self._VehicleGunRotator__ROTATION_TICK_LENGTH)
        self._VehicleGunRotator__updateGunMatrix(self._VehicleGunRotator__gunPitch, self._VehicleGunRotator__ROTATION_TICK_LENGTH)

        diff = abs(estimatedTurretYaw - prevTurretYaw)
        if diff > pi:
            diff = 2 * pi - diff
        self._VehicleGunRotator__turretRotationSpeed = diff / timeDiff

        if g_oneTickCache.shouldBoostTickRate:
            self._VehicleGunRotator__dispersionAngles = getOwnVehicleShotDispersionAngleForGunRotator(self)
        else:
            self._VehicleGunRotator__dispersionAngles = avatar.getOwnVehicleShotDispersionAngle(self._VehicleGunRotator__turretRotationSpeed)


old_VGR_init = VehicleGunRotator.__init__


@overrideIn(VehicleGunRotator)
def __init__(self, *args, **kwargs):
    old_VGR_init(self, *args, **kwargs)

    self._mod_dispersion_state = DispersionState(lastTime=BigWorld.time(),
                                                 lastTurretYaw=self.turretYaw,
                                                 dispersionAngles=self._VehicleGunRotator__dispersionAngles)

    self._mod_lastTurretYawReplayTime = 0.0
    self._mod_lastTurretPitchReplayTime = 0.0

    self._mod_last_syncWithServerTurretYaw_time = 0.0

    self._mod_last_updateGunMarkers_time = 0.0


class DispersionState(object):

    def __init__(self, lastTime, lastTurretYaw, dispersionAngles):
        self.lastTime = lastTime
        self.lastTurretYaw = lastTurretYaw
        self.dispersionAngles = dispersionAngles

    def setState(self, lastTime, lastTurretYaw, dispersionAngles):
        self.lastTime = lastTime
        self.lastTurretYaw = lastTurretYaw
        self.dispersionAngles = dispersionAngles


# performance note: this optimises itself by design
# described on method __rotate above
#
# performance note
#
# normally takes around 110 us on my machine
# with cache - around 2 us
def getOwnVehicleShotDispersionAngleForGunRotator(self):
    dispersionState = self._mod_dispersion_state  # type: Optional[DispersionState]

    timeDiff = BigWorld.time() - dispersionState.lastTime
    if timeDiff < constants.SERVER_TICK_LENGTH:
        return dispersionState.dispersionAngles

    turretYaw = self.turretYaw

    # simulate slower dispersion state update by using cached last turret yaw
    # using similar code that is in gun rotator __rotate method
    turretYawDiff = abs(turretYaw - dispersionState.lastTurretYaw)
    if turretYawDiff > pi:
        turretYawDiff = 2 * pi - turretYawDiff
    newTurretRotationSpeed = turretYawDiff / timeDiff

    avatar = BigWorld.player()  # type: PlayerAvatar
    dispersionAngles = avatar.getOwnVehicleShotDispersionAngle(newTurretRotationSpeed)
    dispersionState.setState(lastTime=BigWorld.time(),
                             lastTurretYaw=turretYaw,
                             dispersionAngles=dispersionAngles)
    return dispersionAngles


old_VGR_updateTurretMatrix = VehicleGunRotator._VehicleGunRotator__updateTurretMatrix


@overrideIn(VehicleGunRotator)
def __updateTurretMatrix(self, yaw, time):
    if not g_oneTickCache.isDuringVgrTick or not g_oneTickCache.shouldBoostTickRate:
        return old_VGR_updateTurretMatrix(self, yaw, time)

    if not self._VehicleGunRotator__isStarted:
        return
    else:
        replayYaw = yaw
        staticTurretYaw = self._VehicleGunRotator__getTurretStaticYaw()
        if staticTurretYaw is not None:
            yaw = staticTurretYaw
        m = Math.Matrix()
        m.setRotateY(yaw)
        self._VehicleGunRotator__turretMatrixAnimator.update(m, 0.0)

        # performance note
        #
        # this saves us around 10 us on average
        bwTime = BigWorld.time()
        timeDiff = bwTime - self._mod_lastTurretYawReplayTime
        if timeDiff < constants.SERVER_TICK_LENGTH:
            return

        self._mod_lastTurretYawReplayTime = bwTime

        replayCtrl = BattleReplay.g_replayCtrl
        if replayCtrl.isRecording:
            replayCtrl.setTurretYaw(replayYaw)


old_VGR_updateGunMatrix = VehicleGunRotator._VehicleGunRotator__updateGunMatrix


@overrideIn(VehicleGunRotator)
def __updateGunMatrix(self, pitch, time):
    if not g_oneTickCache.isDuringVgrTick or not g_oneTickCache.shouldBoostTickRate:
        return old_VGR_updateGunMatrix(self, pitch, time)

    if not self._VehicleGunRotator__isStarted:
        return
    else:
        replayPitch = pitch
        descr = self._avatar.getVehicleDescriptor()
        pitch -= calcGunPitchCorrection(self._VehicleGunRotator__turretYaw, descr.hull.turretPitches[0], descr.turret.gunJointPitch)
        staticPitch = self._VehicleGunRotator__getGunStaticPitch()
        if staticPitch is not None:
            pitch = staticPitch
        m = Math.Matrix()
        m.setRotateX(pitch)
        self._VehicleGunRotator__gunMatrixAnimator.update(m, 0.0)

        # performance note
        #
        # this saves us around 10 us on average
        bwTime = BigWorld.time()
        timeDiff = bwTime - self._mod_lastTurretPitchReplayTime
        if timeDiff < constants.SERVER_TICK_LENGTH:
            return

        self._mod_lastTurretPitchReplayTime = bwTime

        replayCtrl = BattleReplay.g_replayCtrl
        if replayCtrl.isRecording:
            replayCtrl.setGunPitch(replayPitch)


old_VGR_syncWithServerTurretYaw = VehicleGunRotator._VehicleGunRotator__syncWithServerTurretYaw


# performance note
#
# this saves us 10 us on average, because we don't need to be synced with server turret yaw that often

@overrideIn(VehicleGunRotator)
def __syncWithServerTurretYaw(self, estimatedTurretYaw):
    if not g_oneTickCache.isDuringVgrTick or not g_oneTickCache.shouldBoostTickRate:
        return old_VGR_syncWithServerTurretYaw(self, estimatedTurretYaw)

    time = BigWorld.time()
    if (time - self._mod_last_syncWithServerTurretYaw_time) < constants.SERVER_TICK_LENGTH:
        return estimatedTurretYaw

    self._mod_last_syncWithServerTurretYaw_time = time

    return old_VGR_syncWithServerTurretYaw(self, estimatedTurretYaw)


old_VGR_updateGunMarker = VehicleGunRotator._VehicleGunRotator__updateGunMarker


# performance note
#
# on vanilla:
# * VGR AT: 220 us
# * VGR AE: 545 us
#
# modded:
# * VGR AT: 67 us
# * VGR AE: 75 us
#
# very nice!
#
# also WG and Lesta specific
# differences around getBigWorld.wg_getCappedShotTargetInfos

@overrideIn(VehicleGunRotator, condition=isClientWG)
def __updateGunMarker(self, forceRelaxTime=None):
    if not g_oneTickCache.isDuringVgrTick or not g_oneTickCache.shouldBoostTickRate:
        return old_VGR_updateGunMarker(self, forceRelaxTime)

    vehicle = self._avatar.getVehicleAttached()
    if vehicle is None:
        return

    # shouldBoostTickRate == True guarantees controller has _gunMarker field
    gunMarker = self._avatar.inputHandler.ctrl._gunMarker

    # for a cache missed frame (every 100 ms) update gun marker the normal way
    # this triggers all crucial vanilla logic of updating reticle size, replays handling, all other event, etc
    time = BigWorld.time()
    if (time - self._mod_last_updateGunMarkers_time) >= constants.SERVER_TICK_LENGTH:
        self._mod_last_updateGunMarkers_time = time

        newForceRelaxTime = constants.SERVER_TICK_LENGTH if forceRelaxTime is None else forceRelaxTime
        old_VGR_updateGunMarker(self, newForceRelaxTime)

        # we must setPosition again after vanilla reticle logic
        # because reticle position animator just received new position with relaxTime for 100 ms tick
        # and that would animate position from previous *old* position to new one using that relaxTime,
        # which would cause reticle to be one tick behind every 100 ms for that rendering frame,
        # and we DO NOT want that - we want instant reticle position
        #
        # reapply calculated reticle position to skip position animator instantly to the end
        position = gunMarker.getPosition(GUN_MARKER_TYPE.CLIENT)
        gunMarker.setPosition(position, GUN_MARKER_TYPE.CLIENT)

        # remember to update dual accuracy as well
        if vehicle and vehicle.typeDescriptor and vehicle.typeDescriptor.hasDualAccuracy:
            position = gunMarker.getPosition(GUN_MARKER_TYPE.DUAL_ACC)
            gunMarker.setPosition(position, GUN_MARKER_TYPE.DUAL_ACC)

        return

    # during in-between calls of responsive reticle, for performance reasons, as a bare minimum
    # we must update position provider of gun marker data provider
    # so flash reticles with assigned data providers in GunMarkerComponent._setupDataProvider()
    # will always have fresh reticle position
    #
    # to do that, those calls below are a bare minimum vanilla code to achieve that
    # without breaking too much code

    shotPos, shotVec = self.getCurShotPosition()
    shotDescr = self._avatar.getVehicleDescriptor().shot

    minBounds, maxBounds = BigWorld.player().arena.getSpaceBB()
    position = BigWorld.wg_getCappedShotTargetInfos(
        BigWorld.player().spaceID,
        shotPos, shotVec, Math.Vector3(0.0, -shotDescr.gravity, 0.0), shotDescr.maxDistance,
        self.getAttachedVehicleID(),
        minBounds, maxBounds,
        AimingSystems.CollisionStrategy.COLLIDE_DYNAMIC_AND_STATIC
    )[0]

    gunMarker.setPosition(position, GUN_MARKER_TYPE.CLIENT)

    # remember to update dual accuracy as well
    if vehicle and vehicle.typeDescriptor and vehicle.typeDescriptor.hasDualAccuracy:
        gunMarker.setPosition(position, GUN_MARKER_TYPE.DUAL_ACC)


@overrideIn(VehicleGunRotator, condition=isClientLesta)
def __updateGunMarker(self, forceRelaxTime=None):
    if not g_oneTickCache.isDuringVgrTick or not g_oneTickCache.shouldBoostTickRate:
        return old_VGR_updateGunMarker(self, forceRelaxTime)

    vehicle = self._avatar.getVehicleAttached()
    if vehicle is None:
        return

    # shouldBoostTickRate == True guarantees controller has _gunMarker field
    gunMarker = self._avatar.inputHandler.ctrl._gunMarker

    # for a cache missed frame (every 100 ms) update gun marker the normal way
    # this triggers all crucial vanilla logic of updating reticle size, replays handling, all other event, etc
    time = BigWorld.time()
    if (time - self._mod_last_updateGunMarkers_time) >= constants.SERVER_TICK_LENGTH:
        self._mod_last_updateGunMarkers_time = time

        newForceRelaxTime = constants.SERVER_TICK_LENGTH if forceRelaxTime is None else forceRelaxTime
        old_VGR_updateGunMarker(self, newForceRelaxTime)

        # we must setPosition again after vanilla reticle logic
        # because reticle position animator just received new position with relaxTime for 100 ms tick
        # and that would animate position from previous *old* position to new one using that relaxTime,
        # which would cause reticle to be one tick behind every 100 ms for that rendering frame,
        # and we DO NOT want that - we want instant reticle position
        #
        # reapply calculated reticle position to skip position animator instantly to the end
        position = gunMarker.getPosition(GUN_MARKER_TYPE.CLIENT)
        gunMarker.setPosition(position, GUN_MARKER_TYPE.CLIENT)

        # remember to update dual accuracy as well
        if vehicle and vehicle.typeDescriptor and vehicle.typeDescriptor.hasDualAccuracy:
            position = gunMarker.getPosition(GUN_MARKER_TYPE.DUAL_ACC)
            gunMarker.setPosition(position, GUN_MARKER_TYPE.DUAL_ACC)

        return

    # during in-between calls of responsive reticle, for performance reasons, as a bare minimum
    # we must update position provider of gun marker data provider
    # so flash reticles with assigned data providers in GunMarkerComponent._setupDataProvider()
    # will always have fresh reticle position
    #
    # to do that, those calls below are a bare minimum vanilla code to achieve that
    # without breaking too much code

    shotPos, shotVec = self.getCurShotPosition()
    shotDescr = self._avatar.getVehicleDescriptor().shot

    minBounds, maxBounds = BigWorld.player().arena.getSpaceBB()
    position = BigWorld.getCappedShotTargetInfos(
        BigWorld.player().spaceID,
        shotPos, shotVec.scale(shotDescr.speed), Math.Vector3(0.0, -shotDescr.gravity, 0.0), shotDescr.maxDistance,
        self.getAttachedVehicleID(),
        minBounds, maxBounds,
        AimingSystems.CollisionStrategy.COLLIDE_DYNAMIC_AND_STATIC
    )[0]

    gunMarker.setPosition(position, GUN_MARKER_TYPE.CLIENT)

    # remember to update dual accuracy as well
    if vehicle and vehicle.typeDescriptor and vehicle.typeDescriptor.hasDualAccuracy:
        gunMarker.setPosition(position, GUN_MARKER_TYPE.DUAL_ACC)


old_setAimingMode = _GunControlMode.setAimingMode


@overrideIn(_GunControlMode)
def setAimingMode(self, enable, mode):
    if mode == constants.AIMING_MODE.SHOOTING:
        return

    old_setAimingMode(self, enable, mode)


# performance note
#
# without g_oneTickCache:
# * VGR AT: cumulative time is 19.8 us (2 calls)
# * VGR AE: cumulative time is 32.5 us (3 calls)
#
# with g_oneTickCache:
# * on VGR AT and AE tick: cumulative time is 7.1 us (loading call) + 0.4 us (2 calls) = 7.5 us
#
# so this saves (12.3 us; 25 us) on average
@overrideIn(VehicleGunRotator)
def getAvatarOwnVehicleStabilisedMatrix(self):
    if not g_oneTickCache.isDuringVgrTick:
        return old_VGR_getAvatarOwnVehicleStabilisedMatrix(self)

    return g_oneTickCache.gunRotator_avatarOwnVehicleStabilisedMatrix
