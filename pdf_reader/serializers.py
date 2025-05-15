# backend/pdf_reader/serializers.py
from rest_framework import serializers

class FileUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

class MultiFileUploadSerializer(serializers.Serializer):
    # The 'files' key here must match the key used in FormData on the frontend
    files = serializers.ListField(
        child=serializers.FileField(allow_empty_file=False, use_url=False),
        allow_empty=False,
        max_length=25  # Optional: Limit the number of files per request
    )