# backend/pdf_reader/urls.py
from django.urls import path
from .views import CheckDocumentView

urlpatterns = [
    path('check-document/', CheckDocumentView.as_view(), name='check-document'),
]