from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    PropertyViewSet, BookingViewSet,
    CalendarView, DashboardView, AvailabilityCheckView, ReportView,
    LoginView, LogoutView, MeView,
)

router = DefaultRouter()
router.register('properties', PropertyViewSet, basename='property')
router.register('bookings', BookingViewSet, basename='booking')
# NOTE: 'slots' route removed — SlotViewSet/Slot no longer exist as a
# standalone resource. Slots are now created/edited as part of the
# Property payload (see PropertySerializer.slots) and read via
# property.slots in the API response. You'll need to delete the
# SlotViewSet class from views.py once you share that file.

urlpatterns = [
    path('', include(router.urls)),
    path('calendar/', CalendarView.as_view(), name='calendar'),
    path('dashboard/', DashboardView.as_view(), name='dashboard'),
    path('availability-check/', AvailabilityCheckView.as_view(), name='availability-check'),
    path('reports/', ReportView.as_view(), name='reports'),
    path('login/', LoginView.as_view(), name='auth-login'),
    path('logout/', LogoutView.as_view(), name='auth-logout'),
    path('me/', MeView.as_view(), name='auth-me'),
]