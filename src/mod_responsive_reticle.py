import logging

import BattleReplay
import BigWorld
import constants
from Avatar import PlayerAvatar
from AvatarInputHandler.gun_marker_ctrl import _GunMarkerController
from VehicleGunRotator import VehicleGunRotator


log = logging.getLogger(__name__)


def overrideIn(cls, staticMethod=False):

    def _overrideMethod(func):
        funcName = func.__name__

        if funcName.startswith("__") and funcName != "__init__":
            funcName = "_" + cls.__name__ + funcName

        old = getattr(cls, funcName)

        if staticMethod:
            @staticmethod
            def wrapper(*args, **kwargs):
                return func(old, *args, **kwargs)
        else:
            def wrapper(*args, **kwargs):
                return func(old, *args, **kwargs)

        setattr(cls, funcName, wrapper)
        return wrapper
    return _overrideMethod


# __ROTATION_TICK_LENGTH controls, how fast gun rotator updates vehicle turret rotation and gun markers
# with relaxTime * 2 and __ROTATION_TICK_LENGTH = 0.003, there will be 6 ms of total input lag
#
# __INSUFFICIENT_TIME_DIFF controls, when game cannot keep up, how small the time diff must be
# before rotation update can be performed
# because by default it is 0.02 (where rotation __ROTATION_TICK_LENGTH was 0.1), we have to lower it some reasonably
# to make reticle movement less stuttering

def shouldBoostTickRate():
    # we don't want to change tick rate when we're displaying replay
    if BattleReplay.isPlaying():
        return False

    player = BigWorld.player()  # type: PlayerAvatar

    veh = BigWorld.entity(BigWorld.player().playerVehicleID)
    if veh is None:
        return False

    # we don't want to change SPGs gun tick rate because it breaks top-down view reticle dots
    # and this mod is not useful for SPGs, so it's not an issue
    vehTypeDesc = veh.typeDescriptor.type
    if 'SPG' in vehTypeDesc.tags:
        return False

    if player.gunRotator is None:
        return False

    # we don't want to change tick rate during auto-aiming
    # because then reticle size would shrink faster than it should
    return player.gunRotator.clientMode


@overrideIn(VehicleGunRotator)
def __onTick(func, self):
    if shouldBoostTickRate():
        VehicleGunRotator._VehicleGunRotator__ROTATION_TICK_LENGTH = 0.003
        VehicleGunRotator._VehicleGunRotator__INSUFFICIENT_TIME_DIFF = 0.001
    else:
        VehicleGunRotator._VehicleGunRotator__ROTATION_TICK_LENGTH = constants.SERVER_TICK_LENGTH
        VehicleGunRotator._VehicleGunRotator__INSUFFICIENT_TIME_DIFF = 0.02

    func(self)


@overrideIn(_GunMarkerController)
def _updateMatrixProvider(func, self, positionMatrix, relaxTime=0.0):
    if shouldBoostTickRate() and relaxTime == VehicleGunRotator._VehicleGunRotator__ROTATION_TICK_LENGTH:
        # when ROTATION_TICK_LENGTH is small enough, then reticle movement becomes more jagged,
        # and it is like that even despite surrounding code properly interpolating it
        # I even did manual interpolation just to exclude potential Math.MatrixAnimation() flaw or something
        #
        # however
        # when we make ROTATION_TICK_LENGTH smaller and relaxTime bigger than desired values
        # then reticle movement for some unknown fucking reason is noticeably smoother than simply leaving it alone
        #
        # why?
        #
        # is it due to reticle updates frequently happening AFTER interpolation finishes destination (in result,
        # reticle stops for a split of a time)?
        # does increasing relaxTime relative to ROTATION_TICK_LENGTH act as a buffer
        # for update inconsistencies just to make reticle almost always moving?
        #
        # due to this, we have to make position updates slightly more often than relaxTime
        # just to get desired input lag with reduced stuttering
        relaxTime *= 2.0
    func(self, positionMatrix, relaxTime)
