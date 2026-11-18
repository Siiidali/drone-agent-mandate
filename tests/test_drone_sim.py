import pytest

from drone_agent_mandate.drone import DroneError, SimDrone
from drone_agent_mandate.geo import haversine_m, offset_latlon


def test_offset_and_haversine_are_consistent():
    lat, lon = offset_latlon(-35.363261, 149.165230, north_m=30, east_m=40)
    assert haversine_m(-35.363261, 149.165230, lat, lon) == pytest.approx(50, abs=0.1)


def test_full_flight_sequence():
    d = SimDrone()
    home = d.home()
    d.takeoff(10)
    assert d.get_state().armed and d.get_state().alt_m == 10 and d.get_state().mode == "GUIDED"
    target = offset_latlon(*home, north_m=50, east_m=0)
    d.goto(*target, 10)
    st = d.get_state()
    assert haversine_m(st.lat, st.lon, *target) < 0.01
    t_before = d.now()
    d.rtl()
    st = d.get_state()
    assert not st.armed and st.alt_m == 0 and (st.lat, st.lon) == home
    assert d.now() > t_before            # l'horloge simulée avance
    assert st.battery_pct < 100


def test_goto_refused_on_ground():
    with pytest.raises(DroneError):
        SimDrone().goto(-35.36, 149.16, 10)


def test_takeoff_refused_when_flying():
    d = SimDrone()
    d.takeoff(10)
    with pytest.raises(DroneError):
        d.takeoff(20)


def test_home_is_reset_on_arming_like_arducopter():
    """Atterrir en A puis redécoller : le home devient A, donc RTL ramène en A."""
    d = SimDrone()
    start = d.home()
    a = offset_latlon(*start, north_m=30, east_m=0)
    d.takeoff(10)
    d.goto(*a, 10)
    d.land()
    d.takeoff(10)
    assert d.home() == a
    d.goto(*start, 10)
    d.rtl()
    assert (d.get_state().lat, d.get_state().lon) == a
