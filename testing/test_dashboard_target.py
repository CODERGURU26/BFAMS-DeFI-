from unittest.mock import patch

from app.routes import testing_dashboard


def test_staging_dashboard_uses_staging_labels_and_disables_registration():
    with (
        patch.object(testing_dashboard.settings, "app_database_target", "main"),
        patch.object(testing_dashboard, "registration_enabled", return_value=False),
    ):
        response = testing_dashboard.dashboard()

    html = response.body.decode("utf-8")
    assert "STAGING DATABASE" in html
    assert "TEST DATABASE" not in html
    assert '<form id="auth-register" class="auth-form" hidden>' in html


def test_product_dashboard_uses_product_data_route():
    with patch.object(testing_dashboard, "registration_enabled", return_value=True):
        response = testing_dashboard.product_dashboard()

    html = response.body.decode("utf-8")
    assert "/dashboard/data" in html
    assert "/testing/dashboard/data" not in html
    assert "/dashboard/allocation-targets" in html
    assert "/testing/dashboard/allocation-targets" not in html
    assert "Target allocation" in html
