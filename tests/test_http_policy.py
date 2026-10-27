from weekly_projections.web.http_policy import cache_control_for


def test_cache_policy_keeps_private_pages_private_and_versions_static_assets():
    assert cache_control_for("/home") == "no-store"
    assert cache_control_for("/api/command-center") == "no-store"
    assert cache_control_for("/static/app.css") == "public, max-age=3600"
    assert cache_control_for("/static/app.css", versioned=True) == (
        "public, max-age=31536000, immutable"
    )
    assert cache_control_for("/service-worker.js") == "no-cache"
