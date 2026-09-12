from pathlib import Path


def test_subscription_callbacks_are_present():
    catalog = Path("app/handlers/plan_catalog.py").read_text(encoding="utf-8")
    redirect = Path("app/handlers/plan_legacy_redirect.py").read_text(encoding="utf-8")
    billing = Path("app/handlers/billing.py").read_text(encoding="utf-8")
    tasks = Path("app/tasks.py").read_text(encoding="utf-8")
    dashboard = Path("app/handlers/dashboard.py").read_text(encoding="utf-8")
    assert "plans_history:" in catalog
    assert "plans_catalog:compare" in catalog
    assert "plan_checkout:" in catalog
    assert "plan_buy:" in redirect
    assert 'parts[0] == "payment"' in billing
    assert "send_subscription_notices" in tasks
    assert "service:subscriptions" in dashboard
    assert "service_plan_grant:" in dashboard
