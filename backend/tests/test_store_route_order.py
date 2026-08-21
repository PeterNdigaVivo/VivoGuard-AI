from app.api.stores import router


def test_static_zone_health_route_is_not_captured_as_store_id():
    dynamic = next(route for route in router.routes
                   if route.path == "/stores/{store_id:int}")
    zone_health = next(route for route in router.routes
                       if route.path == "/stores/zone-health")

    assert dynamic.path_regex.fullmatch("/stores/zone-health") is None
    assert zone_health.path_regex.fullmatch("/stores/zone-health") is not None
