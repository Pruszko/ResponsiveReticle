import logging
from math import pi
from typing import Optional

import BattleReplay
import BigWorld
import constants
from Avatar import PlayerAvatar
from AvatarInputHandler.gun_marker_ctrl import _GunMarkerController
from GUI import WGGunMarkerDataProvider
from VehicleGunRotator import VehicleGunRotator
from gun_rotation_shared import calcPitchLimitsFromDesc
from items.vehicles import VehicleDescriptor
from projectile_trajectory import getShotAngles

log = logging.getLogger(__name__)


def overrideIn(cls):

    def _overrideMethod(func):
        funcName = func.__name__

        if funcName.startswith("__") and funcName != "__init__":
            funcName = "_" + cls.__name__ + funcName

        old = getattr(cls, funcName)

        def wrapper(*args, **kwargs):
            return func(old, *args, **kwargs)

        setattr(cls, funcName, wrapper)
        return wrapper
    return _overrideMethod


def shouldBoostTickRate():
    # we don't want to change tick rate when we're displaying replay
    if BattleReplay.isPlaying():
        return False

    player = BigWorld.player()  # type: PlayerAvatar

    veh = BigWorld.entity(player.playerVehicleID)
    if veh is None:
        return False

    # we don't want to change SPGs gun tick rate because it breaks top-down view reticle dots
    # and this mod is not useful for SPGs, so it's not an issue
    vehicleDescriptor = veh.typeDescriptor  # type: VehicleDescriptor
    if 'SPG' in vehicleDescriptor.type.tags:
        return False

    # we don't want to change gun tick rate for vehicles that have static gun yaw (for example Strv 103B)
    # because it already has hull-controlled reticle movement
    # and because reticle blinks horribly due to 0/0 gun angles
    return vehicleDescriptor.gun.staticTurretYaw != 0


# __ROTATION_TICK_LENGTH controls, how fast gun rotator updates vehicle turret rotation and gun markers
#
# __INSUFFICIENT_TIME_DIFF controls how small the time diff must be before rotation update can be performed
# because by default it is 0.02 (where rotation __ROTATION_TICK_LENGTH was 0.1), we have to lower it some reasonably
# to allow faster tick-rate

@overrideIn(VehicleGunRotator)
def __onTick(func, self):
    if shouldBoostTickRate():
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

    func(self)


@overrideIn(_GunMarkerController)
def _updateMatrixProvider(func, self, positionMatrix, relaxTime=0.0):
    # second check makes sure, we alter relaxTime only for client-side reticle code - not the server one
    if shouldBoostTickRate() and relaxTime == VehicleGunRotator._VehicleGunRotator__ROTATION_TICK_LENGTH:
        # when ROTATION_TICK_LENGTH is quite small (like 0.006), then reticle movement stutters,
        # and it is like that even despite surrounding code properly interpolating it
        # I even did my own manual interpolation just to exclude potential Math.MatrixAnimation() flaw or something
        #
        # generally interpolation in games doesn't work well when tick-rate is very fast
        # due to time variance, distance variation (which causes movement oscillation)
        # and randomly gives period of time, where position is not interpolated due to finished destination
        #
        # so - for such high tick-rate it is better to remove interpolation
        # and trigger next reticle position every frame (its still fast code, so we can do that)
        relaxTime = 0

    func(self, positionMatrix, relaxTime)


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
@overrideIn(VehicleGunRotator)
def __rotate(func, self, shotPoint, timeDiff):
    self._VehicleGunRotator__turretRotationSpeed = 0.0
    targetPoint = shotPoint if shotPoint is not None else self._VehicleGunRotator__prevSentShotPoint
    replayCtrl = BattleReplay.g_replayCtrl
    if targetPoint is None or self._VehicleGunRotator__isLocked and not replayCtrl.isUpdateGunOnTimeWarp:
        if shouldBoostTickRate():
            self._VehicleGunRotator__dispersionAngles = getOwnVehicleShotDispersionAngleForGunRotator(self, 0.0)
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
        if not replayCtrl.isPlaying:
            self._VehicleGunRotator__turretYaw = turretYaw = self._VehicleGunRotator__syncWithServerTurretYaw(estimatedTurretYaw)
        else:
            self._VehicleGunRotator__turretYaw = turretYaw = estimatedTurretYaw
        if maxTurretRotationSpeed != 0:
            self.estimatedTurretRotationTime = abs(turretYaw - shotTurretYaw) / maxTurretRotationSpeed
        else:
            self.estimatedTurretRotationTime = 0
        gunPitchLimits = calcPitchLimitsFromDesc(turretYaw, self._VehicleGunRotator__getGunPitchLimits(),
                                                 descr.hull.turretPitches[0], descr.turret.gunJointPitch)
        self._VehicleGunRotator__gunPitch = self.getNextGunPitch(self._VehicleGunRotator__gunPitch, shotGunPitch, timeDiff, gunPitchLimits)
        if replayCtrl.isPlaying and replayCtrl.isUpdateGunOnTimeWarp:
            self._VehicleGunRotator__updateTurretMatrix(turretYaw, 0.0)
            self._VehicleGunRotator__updateGunMatrix(self._VehicleGunRotator__gunPitch, 0.0)
        else:
            self._VehicleGunRotator__updateTurretMatrix(turretYaw, self._VehicleGunRotator__ROTATION_TICK_LENGTH)
            self._VehicleGunRotator__updateGunMatrix(self._VehicleGunRotator__gunPitch, self._VehicleGunRotator__ROTATION_TICK_LENGTH)
        diff = abs(estimatedTurretYaw - prevTurretYaw)
        if diff > pi:
            diff = 2 * pi - diff
        self._VehicleGunRotator__turretRotationSpeed = diff / timeDiff

        if shouldBoostTickRate():
            self._VehicleGunRotator__dispersionAngles = getOwnVehicleShotDispersionAngleForGunRotator(self, self._VehicleGunRotator__turretRotationSpeed)
        else:
            self._VehicleGunRotator__dispersionAngles = avatar.getOwnVehicleShotDispersionAngle(self._VehicleGunRotator__turretRotationSpeed)


class DispersionState(object):

    def __init__(self, lastTime, lastTurretYaw, dispersionAngles):
        self.lastTime = lastTime
        self.lastTurretYaw = lastTurretYaw
        self.dispersionAngles = dispersionAngles

    def setState(self, lastTime, lastTurretYaw, dispersionAngles):
        self.lastTime = lastTime
        self.lastTurretYaw = lastTurretYaw
        self.dispersionAngles = dispersionAngles


def getOwnVehicleShotDispersionAngleForGunRotator(gunRotator, turretRotationSpeed):
    avatar = BigWorld.player()  # type: PlayerAvatar
    gunRotator = gunRotator  # type: VehicleGunRotator

    turretYaw = gunRotator.turretYaw

    # cache current state if not exists
    dispersionState = getattr(gunRotator, "_mod_dispersion_state", None)  # type: Optional[DispersionState]
    if dispersionState is None:
        dispersionAngles = avatar.getOwnVehicleShotDispersionAngle(turretRotationSpeed)

        gunRotator._mod_dispersion_state = DispersionState(lastTime=BigWorld.time(),
                                                           lastTurretYaw=turretYaw,
                                                           dispersionAngles=dispersionAngles)
        return dispersionAngles

    # return last cached dispersion angles (and overall ignore fast consecutive dispersion state updates)
    timeDiff = BigWorld.time() - dispersionState.lastTime
    if timeDiff < constants.SERVER_TICK_LENGTH:
        return dispersionState.dispersionAngles

    # simulate slower dispersion state update by using cached last turret yaw
    # using similar code that is in gun rotator __rotate method
    turretYawDiff = abs(turretYaw - dispersionState.lastTurretYaw)
    if turretYawDiff > pi:
        turretYawDiff = 2 * pi - turretYawDiff
    newTurretRotationSpeed = turretYawDiff / timeDiff

    dispersionAngles = avatar.getOwnVehicleShotDispersionAngle(newTurretRotationSpeed)
    dispersionState.setState(lastTime=BigWorld.time(),
                             lastTurretYaw=turretYaw,
                             dispersionAngles=dispersionAngles)
    return dispersionAngles


# the best place to handle reticle size interpolation would be in _DefaultGunMarkerController
# however - that class is very commonly either overridden or replaced completely by server-reticle related mods
# so the next good place to do that is at the data provider directly

@overrideIn(WGGunMarkerDataProvider)
def updateSizes(func, self, currentSize, currentSizeOffset, relaxTime, offsetInertness):
    # second check makes sure, we alter data provider only for client-side data provider - not the server one
    if not shouldBoostTickRate() or relaxTime != VehicleGunRotator._VehicleGunRotator__ROTATION_TICK_LENGTH:
        func(self, currentSize, currentSizeOffset, relaxTime, offsetInertness)
        return

    # we cannot add attributes to data provider, because it is python binding object that doesn't have it enabled :(
    # so we must track method calls somewhere outside
    # and I want it to be cleared somewhere automatically without doing overrides
    # so let's just store it in player object and call it a day
    player = BigWorld.player()

    dataProviderSizeCache = getattr(player, "_mod_dataProviderSizeCache", None)  # type: dict
    if dataProviderSizeCache is None:
        dataProviderSizeCache = {}
        player._mod_dataProviderSizeCache = dataProviderSizeCache

    selfId = id(self)
    lastTime = dataProviderSizeCache.get(selfId, None)
    if lastTime is None:
        dataProviderSizeCache[selfId] = BigWorld.time()
        func(self, currentSize, currentSizeOffset, constants.SERVER_TICK_LENGTH, offsetInertness)
        return

    # ignore fast consecutive size updates in data provider
    timeDiff = BigWorld.time() - lastTime
    if timeDiff < constants.SERVER_TICK_LENGTH:
        return

    dataProviderSizeCache[selfId] = BigWorld.time()
    func(self, currentSize, currentSizeOffset, constants.SERVER_TICK_LENGTH, offsetInertness)
