from Avatar import PlayerAvatar
from avatar_components.AvatarObserver import AvatarObserver

from responsive_reticle.helpers.one_tick_cache import g_oneTickCache, old_getVehicleAttached, \
    old_avatar_getVehicleDescriptor, old_avatar_getOwnVehicleStabilisedMatrix
from responsive_reticle.utils import overrideIn, isClientWG, isClientLesta


# performance note
#
# without g_oneTickCache:
# * VGR AT: cumulative time is 21 us (8 calls)
# * VGR AE: cumulative time is 28 us (10 calls)
#
# with g_oneTickCache:
# * on VGR AT and AE tick: cumulative time is 7.5 us (loading call) + 0.5 us (2 calls) = 8 us
#
# so this saves (13 us; 20 us) on average
#
# also WG and Lesta specific
# different method location

@overrideIn(PlayerAvatar, condition=isClientWG)
def getVehicleAttached(self):
    if not g_oneTickCache.isDuringVgrTick:
        return old_getVehicleAttached(self)

    return g_oneTickCache.avatar_vehicleAttached


@overrideIn(AvatarObserver, condition=isClientLesta)
def getVehicleAttached(self):
    if not g_oneTickCache.isDuringVgrTick:
        return old_getVehicleAttached(self)

    return g_oneTickCache.avatar_vehicleAttached


# performance note
#
# without g_oneTickCache:
# * VGR AT: cumulative time is 9.7 us (4 calls)
# * VGR AE: cumulative time is 17.6 us (7 calls)
#
# with g_oneTickCache:
# * on VGR AT and AE tick: cumulative time is 3.2 us (loading call) + 0.7 us (4 calls) = 3.9 us
#
# so this saves (5.8 us; 13.7 us) on average
@overrideIn(PlayerAvatar)
def getVehicleDescriptor(self):
    if not g_oneTickCache.isDuringVgrTick:
        return old_avatar_getVehicleDescriptor(self)

    return g_oneTickCache.avatar_vehicleDescriptor


# performance note
#
# without g_oneTickCache:
# * VGR AT: cumulative time is 10.7 us (3 calls)
# * VGR AE: cumulative time is 18.1 us (5 calls)
#
# with g_oneTickCache:
# * on VGR AT and AE tick: cumulative time is 3.6 us (loading call) + 0.5 us (2 calls) = 4.1 us
#
# so this saves (6.6 us; 14 us) on average
@overrideIn(PlayerAvatar)
def getOwnVehicleStabilisedMatrix(self):
    if not g_oneTickCache.isDuringVgrTick:
        return old_avatar_getOwnVehicleStabilisedMatrix(self)

    return g_oneTickCache.avatar_ownVehicleStabilisedMatrix
