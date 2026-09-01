from typing import Optional

import Math
import BigWorld
from Avatar import PlayerAvatar
from Vehicle import Vehicle
from VehicleGunRotator import VehicleGunRotator
from avatar_components.AvatarObserver import AvatarObserver
from items.vehicles import VehicleDescriptor

from responsive_reticle.utils import isClientWG

# we're putting old methods here instead of near hooks
# because we have circular dependency between hooks and this cache,
#
# and we want to avoid import statements in updateCache()
# because those takes on average extra 10 us from VGR tick
#
# and vice versa for our hooks as well
old_getVehicleAttached = PlayerAvatar.getVehicleAttached if isClientWG() else AvatarObserver.getVehicleAttached
old_avatar_getVehicleDescriptor = PlayerAvatar.getVehicleDescriptor
old_avatar_getOwnVehicleStabilisedMatrix = PlayerAvatar.getOwnVehicleStabilisedMatrix
old_VGR_getAvatarOwnVehicleStabilisedMatrix = VehicleGunRotator.getAvatarOwnVehicleStabilisedMatrix


# we simplify logic of VehicleGunRotator.getAvatarOwnVehicleStabilisedMatrix()
# because normally it includes this call for STRV-like tanks:
#
# if self.__getTurretStaticYaw() is not None and playerVehicle is not None:
#     vehicleMatrix = Math.Matrix(playerVehicle.filter.interpolateStabilisedMatrix(BigWorld.time()))
#
# which doesn't work well when we increase tick-rate (reticle is very stuttery)
# because game internally in BigWorld probably uses constant 100 ms interpolation
# which we cannot alter
#
# we disable this interpolation for STRV-like tanks for increased responsiveness
# at the cost of not so perfectly fluid reticle movement on hull rotation
def getAvatarOwnVehicleStabilisedMatrix(player):
    return Math.Matrix(player.getOwnVehicleStabilisedMatrix())


# those method results in this cache are calculated only once
# and their hooked variants kept as fast as possible
# to keep very fast repeated calls on some a little heavier result calculations
#
# next calls on such methods are around 0.2 us!
class OneTickCache(object):

    def __init__(self):
        self.isDuringVgrTick = False

        self.shouldBoostTickRate = False

        # we want them to be kept as properties instead of methods, because this is fast
        self.gunRotator_avatarOwnVehicleStabilisedMatrix = Math.Matrix()
        self.avatar_ownVehicleStabilisedMatrix = Math.Matrix()
        self.avatar_vehicleAttached = None  # type: Optional[Vehicle]
        self.avatar_vehicleDescriptor = None  # type: Optional[VehicleDescriptor]

    def updateCache(self):
        player = BigWorld.player()

        # by calling them in this order, every next cache value loading uses previous one
        # nice!
        self.avatar_vehicleAttached = old_getVehicleAttached(player)
        self.avatar_vehicleDescriptor = old_avatar_getVehicleDescriptor(player)
        self.avatar_ownVehicleStabilisedMatrix = old_avatar_getOwnVehicleStabilisedMatrix(player)
        self.gunRotator_avatarOwnVehicleStabilisedMatrix = getAvatarOwnVehicleStabilisedMatrix(player)

        # we don't want to boost tick-rate, when current input controller
        # doesn't have gun marker that we want to increase responsiveness of
        self.shouldBoostTickRate = hasattr(BigWorld.player().inputHandler.ctrl, "_gunMarker")


g_oneTickCache = OneTickCache()
