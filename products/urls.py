from django.urls import path

from . import views

app_name = "products"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path(
        "reports/profit-and-loss/",
        views.profit_loss_report,
        name="profit_and_loss",
    ),
]
