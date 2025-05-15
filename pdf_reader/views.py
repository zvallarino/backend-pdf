# backend/pdf_reader/views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
# from .serializers import FileUploadSerializer # Old serializer
from .serializers import MultiFileUploadSerializer # New serializer
import pdfplumber
from docx import Document # Assuming you'll add .docx support
import io
import re
import json
import os
from django.conf import settings
from collections import defaultdict
import traceback


def load_keywords():
    file_path = os.path.join(settings.BASE_DIR, 'pdf_reader', 'keywords.json')
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"ERROR: keywords.json not found at {file_path}")
        return {}
    except json.JSONDecodeError:
        print(f"ERROR: Could not decode keywords.json at {file_path}")
        return {}

WORDS_TO_CHECK = load_keywords()

class CheckDocumentView(APIView):
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request, *args, **kwargs):
        serializer = MultiFileUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        uploaded_files = serializer.validated_data['files']
        results_for_all_files = []

        for uploaded_file in uploaded_files:
            file_name_original = uploaded_file.name
            file_name_lower = file_name_original.lower()
            current_file_result = {
                "filename": file_name_original,
                "status": "pass", # Default for this file
                "fail_summary": [],
                "found_instances": []
            }

            all_found_instances_for_file = []
            keyword_tracking_for_file = defaultdict(lambda: {'count': 0, 'pages': set()})
            # Initialize fail_if_found property for keywords being tracked for this file
            for kw, props in WORDS_TO_CHECK.items():
                 keyword_tracking_for_file[kw]['fail_if_found'] = props.get('fail_if_found', False)


            document_failed_due_to_keyword_for_file = False

            # print(f"\n--- Processing file: {file_name_original} ---")

            try:
                page_texts_to_process = []

                if file_name_lower.endswith('.pdf'):
                    # Ensure the file pointer is at the beginning if reading multiple times or from memory
                    uploaded_file.seek(0)
                    with pdfplumber.open(uploaded_file) as pdf:
                        for page_num, page_obj in enumerate(pdf.pages):
                            page_text = page_obj.extract_text()
                            if not page_text:
                                # print(f"  Page {page_num + 1} ({file_name_original}): No text extracted.")
                                continue
                            # print(f"  --- Page {page_num + 1} Text ({file_name_original}) ---")
                            # print(page_text[:200] + "..." if len(page_text) > 200 else page_text) # Print snippet
                            # print("  ------------------------------------")
                            page_texts_to_process.append((f"Page {page_num + 1}", page_text, page_num + 1))

                elif file_name_lower.endswith('.docx'):
                    uploaded_file.seek(0) # Reset file pointer
                    doc = Document(io.BytesIO(uploaded_file.read()))
                    # print(f"  --- DOCX Content ({file_name_original}) ---")
                    full_doc_text_list = []
                    for para_num, para in enumerate(doc.paragraphs):
                        para_text = para.text
                        # print(f"  Paragraph {para_num + 1}: {para_text[:100]}...") # Optional: print para snippet
                        full_doc_text_list.append(para_text)
                    combined_text = "\n".join(full_doc_text_list)
                    # print(combined_text[:500] + "..." if len(combined_text) > 500 else combined_text) # Print snippet
                    # print("  ------------------------------------")
                    # For DOCX, page_num is 1, page_identifier is "Document"
                    page_texts_to_process.append(("Document", combined_text, 1))
                else:
                    current_file_result["status"] = "error"
                    current_file_result["error_message"] = "Unsupported file type."
                    results_for_all_files.append(current_file_result)
                    # print(f"  Unsupported file type: {file_name_original}")
                    continue # Skip to the next file

                # --- Process extracted text for the current file ---
                for page_identifier_str, text_content, current_page_number_for_tracking in page_texts_to_process:
                    words_in_content = re.findall(r"[\w'-]+|[.,!?;()]", text_content)

                    for i, current_word_in_content in enumerate(words_in_content):
                        cleaned_word = re.sub(r"[,.!?;()]", "", current_word_in_content).lower()
                        if not cleaned_word:
                            continue

                        for keyword_to_check, properties in WORDS_TO_CHECK.items():
                            if keyword_to_check.lower() == cleaned_word:
                                keyword_tracking_for_file[keyword_to_check]['count'] += 1
                                keyword_tracking_for_file[keyword_to_check]['pages'].add(current_page_number_for_tracking)

                                if properties.get("fail_if_found", False):
                                    document_failed_due_to_keyword_for_file = True

                                start_index = max(0, i - 2)
                                end_index = min(len(words_in_content), i + 3)
                                context_list = [words_in_content[k] for k in range(start_index, i)] + \
                                               [current_word_in_content] + \
                                               [words_in_content[k] for k in range(i + 1, end_index)]
                                context_phrase = " ".join(context_list)

                                all_found_instances_for_file.append({
                                    "page": current_page_number_for_tracking,
                                    "word": keyword_to_check,
                                    "phrase": context_phrase,
                                    "original_match": current_word_in_content
                                })

                current_file_result["found_instances"] = all_found_instances_for_file
                fail_summary_report_for_file = []

                if document_failed_due_to_keyword_for_file:
                    current_file_result["status"] = "fail"
                    for kw, data in keyword_tracking_for_file.items():
                        if data['fail_if_found'] and data['count'] > 0:
                            fail_summary_report_for_file.append({
                                "keyword": kw,
                                "count": data['count'],
                                "pages": sorted(list(data['pages']))
                            })
                elif not all_found_instances_for_file: # No keywords found at all
                    current_file_result["status"] = "pass"
                else: # Keywords found, but none that cause a "fail"
                    current_file_result["status"] = "pass"

                current_file_result["fail_summary"] = fail_summary_report_for_file

                # print(f"  --- Final Status for {file_name_original}: {current_file_result['status'].upper()} ---")
                if fail_summary_report_for_file:
                    # print("    Failure Summary:")
                    for item in fail_summary_report_for_file:
                        print(f"      - {item['keyword']} was found {item['count']} times on pages: {', '.join(map(str, item['pages']))}")
                if all_found_instances_for_file:
                    print("    Detailed Instances found:", len(all_found_instances_for_file))
                elif current_file_result['status'] == "pass":
                    print("    No specified keywords found.")
                print(f"  --- Finished processing {file_name_original} ---\n")


            except Exception as e:
                print(f"  Error processing file {file_name_original}: {e}")
                print(traceback.format_exc())
                current_file_result["status"] = "error"
                current_file_result["error_message"] = f"An unexpected error occurred: {str(e)}"

            results_for_all_files.append(current_file_result)

        return Response(results_for_all_files, status=status.HTTP_200_OK)