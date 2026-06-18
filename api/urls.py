from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    PropertyViewSet, BookingViewSet, PaymentViewSet,
    CalendarView, DashboardView, AvailabilityCheckView, ReportView,
    LoginView, LogoutView, MeView,
)

router = DefaultRouter()
router.register('properties', PropertyViewSet, basename='property')
router.register('bookings', BookingViewSet, basename='booking')
router.register('payments', PaymentViewSet, basename='payment')

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