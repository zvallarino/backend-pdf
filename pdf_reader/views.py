# backend/pdf_reader/views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from .serializers import MultiFileUploadSerializer
import pdfplumber
from docx import Document
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
# Pre-compile a regex for tokenizing words on a page for vicinity checks
WORD_TOKENIZER_REGEX = re.compile(r"[\w'-]+")


class CheckDocumentView(APIView):
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request, *args, **kwargs):
        serializer = MultiFileUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        uploaded_files = serializer.validated_data['files']
        results_for_all_files = []
        CONTEXT_WINDOW_CHARS = 60 # Characters before and after match for context phrase

        for uploaded_file in uploaded_files:
            file_name_original = uploaded_file.name
            file_name_lower = file_name_original.lower()
            current_file_result = {
                "filename": file_name_original, "status": "pass",
                "fail_summary": [], "found_instances": [], "error_message": None
            }

            keyword_tracking_for_file = defaultdict(lambda: {'count': 0, 'pages': set(), 'fail_if_found': False})
            for kw, props in WORDS_TO_CHECK.items(): # Initialize all, including trigger words by their name
                keyword_tracking_for_file[kw]['fail_if_found'] = props.get('fail_if_found', False)
                # If vicinity config exists, it will also get its fail_if_found from its own entry.

            document_failed_due_to_keyword_for_file = False
            all_found_instances_for_file_accumulated = []

            try:
                page_texts_to_process = []
                if file_name_lower.endswith('.pdf'):
                    uploaded_file.seek(0)
                    with pdfplumber.open(uploaded_file) as pdf:
                        if not pdf.pages:
                            current_file_result.update({"status": "error", "error_message": "PDF has no pages or could not be read."})
                            results_for_all_files.append(current_file_result)
                            continue
                        for page_num, page_obj in enumerate(pdf.pages):
                            page_text = page_obj.extract_text()
                            if page_text:
                                page_texts_to_process.append((f"Page {page_num + 1}", page_text, page_num + 1))
                elif file_name_lower.endswith('.docx'):
                    uploaded_file.seek(0)
                    try:
                        doc = Document(io.BytesIO(uploaded_file.read()))
                        full_doc_text_list = [para.text for para in doc.paragraphs if para.text]
                        combined_text = "\n".join(full_doc_text_list)
                        if combined_text:
                            page_texts_to_process.append(("Document", combined_text, 1))
                    except Exception as docx_err:
                        current_file_result.update({"status": "error", "error_message": f"Could not read DOCX content: {str(docx_err)}"})
                        results_for_all_files.append(current_file_result)
                        continue
                else:
                    current_file_result.update({"status": "error", "error_message": "Unsupported file type."})
                    results_for_all_files.append(current_file_result)
                    continue

                if not page_texts_to_process and current_file_result["status"] == "pass":
                    pass # Will naturally pass with no found_instances

                for _page_label, text_content, current_page_num_for_tracking in page_texts_to_process:
                    # Tokenize the current page's text_content for vicinity checks
                    page_word_objects = []
                    for match_obj in WORD_TOKENIZER_REGEX.finditer(text_content):
                        page_word_objects.append({
                            'text': match_obj.group(0),
                            'lower': match_obj.group(0).lower(),
                            'start': match_obj.start(),
                            'end': match_obj.end()
                        })
                    
                    if not page_word_objects: continue

                    for keyword_from_json, properties in WORDS_TO_CHECK.items():
                        vicinity_config = properties.get("check_vicinity")

                        if vicinity_config:
                            # --- Logic for Trigger Keywords with Vicinity Check ---
                            trigger_keyword_lower = keyword_from_json.lower()
                            proximity_terms_lower = [term.lower() for term in vicinity_config["terms"]]
                            window = vicinity_config["window"]
                            report_as = vicinity_config.get("report_as_concept", keyword_from_json)

                            for idx, word_obj in enumerate(page_word_objects):
                                if word_obj['lower'] == trigger_keyword_lower:
                                    # Found an instance of the trigger word
                                    scan_start_idx = max(0, idx - window)
                                    # +1 to include the word at end of window, +1 because word_obj is one word
                                    scan_end_idx = min(len(page_word_objects), idx + 1 + window) 
                                    
                                    found_prox_match_details = None
                                    for k in range(scan_start_idx, scan_end_idx):
                                        if k == idx: continue # Skip the trigger word itself
                                        if page_word_objects[k]['lower'] in proximity_terms_lower:
                                            found_prox_match_details = page_word_objects[k]
                                            break
                                    
                                    if found_prox_match_details:
                                        # Conceptual match found!
                                        keyword_to_report = report_as # Use the concept name for tracking if available
                                        
                                        # Update tracking using the main trigger keyword_from_json or report_as
                                        # For simplicity in keyword_tracking_for_file, let's use keyword_from_json
                                        # The fail_summary will then show "breastfeed" (for example)
                                        keyword_tracking_for_file[keyword_from_json]['count'] += 1
                                        keyword_tracking_for_file[keyword_from_json]['pages'].add(current_page_num_for_tracking)

                                        if properties.get("fail_if_found", False):
                                            document_failed_due_to_keyword_for_file = True
                                        
                                        # Construct original_match string
                                        # Order them by appearance in text
                                        first_word_obj = word_obj if word_obj['start'] < found_prox_match_details['start'] else found_prox_match_details
                                        second_word_obj = found_prox_match_details if word_obj['start'] < found_prox_match_details['start'] else word_obj
                                        original_match_text = f"{first_word_obj['text']} ... {second_word_obj['text']}"
                                        
                                        # Determine character span of the conceptual match for context
                                        match_span_start_char = min(word_obj['start'], found_prox_match_details['start'])
                                        match_span_end_char = max(word_obj['end'], found_prox_match_details['end'])

                                        context_start = max(0, match_span_start_char - CONTEXT_WINDOW_CHARS)
                                        context_end = min(len(text_content), match_span_end_char + CONTEXT_WINDOW_CHARS)
                                        context_phrase = text_content[context_start:context_end]
                                        if context_start > 0: context_phrase = "... " + context_phrase
                                        if context_end < len(text_content): context_phrase += " ..."

                                        all_found_instances_for_file_accumulated.append({
                                            "page": current_page_num_for_tracking,
                                            "word": keyword_to_report, # "breastfeed people/person" or just "breastfeed"
                                            "phrase": context_phrase.strip(),
                                            "original_match": original_match_text
                                        })
                        else:
                            # --- Logic for Direct Keyword/Phrase Matching (No Vicinity Check) ---
                            pattern = r'\b' + re.escape(keyword_from_json) + r'\b'
                            try:
                                for match in re.finditer(pattern, text_content, re.IGNORECASE):
                                    original_match_text = match.group(0)
                                    
                                    keyword_tracking_for_file[keyword_from_json]['count'] += 1
                                    keyword_tracking_for_file[keyword_from_json]['pages'].add(current_page_num_for_tracking)

                                    if properties.get("fail_if_found", False):
                                        document_failed_due_to_keyword_for_file = True

                                    start_char_index = match.start()
                                    end_char_index = match.end()
                                    context_start = max(0, start_char_index - CONTEXT_WINDOW_CHARS)
                                    context_end = min(len(text_content), end_char_index + CONTEXT_WINDOW_CHARS)
                                    context_phrase = text_content[context_start:context_end]
                                    if context_start > 0: context_phrase = "... " + context_phrase
                                    if context_end < len(text_content): context_phrase += " ..."
                                    
                                    all_found_instances_for_file_accumulated.append({
                                        "page": current_page_num_for_tracking,
                                        "word": keyword_from_json,
                                        "phrase": context_phrase.strip(),
                                        "original_match": original_match_text
                                    })
                            except re.error: # Should not happen with re.escape but good practice
                                pass 
                
                current_file_result["found_instances"] = all_found_instances_for_file_accumulated
                if document_failed_due_to_keyword_for_file:
                    current_file_result["status"] = "fail"
                    fail_summary_list = []
                    for kw, data in keyword_tracking_for_file.items():
                        if data['fail_if_found'] and data['count'] > 0:
                            fail_summary_list.append({
                                "keyword": kw, # This will be the main trigger keyword (e.g., "breastfeed")
                                "count": data['count'],
                                "pages": sorted(list(data['pages']))
                            })
                    current_file_result["fail_summary"] = fail_summary_list
            
            except Exception as e:
                print(f"  Overall error processing file {file_name_original}: {e}")
                print(traceback.format_exc())
                current_file_result.update({"status": "error", "error_message": f"An unexpected error: {str(e)}"})
            
            results_for_all_files.append(current_file_result)
        return Response(results_for_all_files, status=status.HTTP_200_OK)