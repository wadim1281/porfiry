import os
import uuid
import tempfile
import time
import base64
import requests
import re
import json
import streamlit as st
from streamlit_sortables import sort_items
from st_draggable_list import DraggableList
from streamlit_markdown import st_markdown, st_streaming_markdown
from streamlit_extras.stylable_container import stylable_container
# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# API endpoints
API_LL = "http://localhost:8000"
OCR_URL = "http://127.0.0.1:8001/ocr"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# Page configuration
st.set_page_config(layout="wide", page_title="PORFIRY", page_icon="")


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def clean_base64_images(md: str) -> str:
    """Remove inline base64-encoded images from Markdown."""
    base64_img_pattern = re.compile(r"!\[[^\]]*\]\(data:image/[^)]+\)")
    return base64_img_pattern.sub("", md)

def determine_image_mime_type(filename: str) -> str:
    """Determine MIME type based on file extension."""
    return "png" if filename.lower().endswith(".png") else "jpeg"

def encode_image_to_base64(image_path: str) -> str:
    """Encode image file to base64 string."""
    try:
        with open(image_path, "rb") as file:
            return base64.b64encode(file.read()).decode()
    except Exception:
        return ""

def save_uploaded_file(uploaded_file, temp_dir: str) -> str:
    """Save uploaded file to temporary directory and return path."""
    file_ext = os.path.splitext(uploaded_file.name)[1].lower()
    temp_path = os.path.join(temp_dir, f"{uuid.uuid4().hex}{file_ext}")
    
    with open(temp_path, "wb") as file:
        file.write(uploaded_file.read())
    
    return temp_path

# ═══════════════════════════════════════════════════════════════════════════════
# MARKDOWN UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def fix_markdown_linebreaks(text: str) -> str:
    """Merge stray single line breaks that split sentences or inline code."""
    def replace(match):
        before, after = match.group(1), match.group(2)
        if after in ",.:;!?": return before + after
        return before + " " + after

    # Do not merge newlines that are followed by block-level elements
    # (lists, headings, quotes, tables, and now images).
    pattern = re.compile(r"([^\n])\n(?!\n|\s*(?:[-*+]|\d+\.|#|>|\||!))([^\n])", re.MULTILINE)
    
    out = text
    # Loop to handle multiple passes if needed
    while True:
        new_out, n = pattern.subn(replace, out)
        if n == 0:
            break
        out = new_out
        
    out = re.sub(r"\s+([,.:;?!])", r"\1", out)
    return out

# ═══════════════════════════════════════════════════════════════════════════════
# MARKDOWN PLACEHOLDER RENDERING (streamlit_markdown)
# ═══════════════════════════════════════════════════════════════════════════════

# Helper to render Markdown using the third-party streamlit_markdown library so
# that all Markdown in the application is handled consistently in one place.
# It clears the given placeholder first and then writes the provided text using
# st_markdown which supports extended features like code copy buttons, etc.

def render_markdown(placeholder, text: str, key: str | None = None):
    """Render *text* inside *placeholder* using st_markdown.*

    Each call clears the placeholder and creates a component with a unique `key` (unless passed manually) 
    to avoid StreamlitDuplicateElementKey."""
    placeholder.empty()
    if key is None:
        key = str(uuid.uuid4())
    with placeholder.container():
        st_markdown(text, theme_color="gray", unsafe_allow_html=True, key=key)

# ═══════════════════════════════════════════════════════════════════════════════
# VULNERABILITY REPORT GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════

class VulnReportGenerator:
    """Handles vulnerability report generation functionality."""
    
    def __init__(self):
        self.session_keys = {
            "title": "",
            "shots": [],
            "draft": "",
            "last_md": "",
            "last_md_raw": "",
            "history": [],
            "zoom_open": False,
            "zoom_path": None,
            "restore_open": False,
            "restore_map": {},
            "busy": False,
            "md_placeholder": None,
            "ocr_modal_open": False,
            "ocr_text": "",
            "ocr_streaming": False,
            "current_ocr_path": None,
            "ocr_finished": False,
            "upload_ver": 0,
            "sort_ver": 0,
            "report_type": "VULNERABILITY",
            # Holds draft update that must be applied at start of next run (because we cannot
            # mutate widget value after it is instantiated in the same run).
            "draft_pending": None,
        }
        self._initialize_session_state()
    
    def _initialize_session_state(self):
        """Initialize session state with default values."""
        for key, default_value in self.session_keys.items():
            session_key = f"app1_{key}"
            if session_key not in st.session_state:
                st.session_state[session_key] = default_value
    
    def get_state(self, key: str):
        """Get value from session state."""
        return st.session_state[f"app1_{key}"]
    
    def set_state(self, key: str, value):
        """Set value in session state."""
        st.session_state[f"app1_{key}"] = value
    
    def close_all_modals(self):
        """Close all modal windows."""
        self.set_state("zoom_open", False)
        self.set_state("zoom_path", None)
        self.set_state("restore_open", False)
        self.set_state("ocr_modal_open", False)
        self.set_state("ocr_streaming", False)
        self.set_state("ocr_finished", False)
        self.set_state("ocr_text", "")
        self.set_state("current_ocr_path", None)
    
    def renumber_screenshots(self):
        """Renumber screenshots with sequential names and update placeholders if names change."""
        replacements: dict[str, str] = {}
        for idx, shot in enumerate(self.get_state("shots"), 1):
            file_ext = os.path.splitext(shot["path"])[1].lower()
            old_name = shot["name"]
            new_name = f"screenshot{idx}{file_ext}"
            if old_name and old_name != new_name:
                replacements[old_name] = new_name
            shot["name"] = new_name
        if replacements:
            def _apply_replacements(text: str) -> str:
                if not text:
                    return text
                for old, new in replacements.items():
                    text = text.replace(f"({old})", f"({new})")
                return text
            # Store draft update for next script run to avoid Streamlit widget mutation error
            self.set_state("draft_pending", _apply_replacements(self.get_state("draft")))
            self.set_state("last_md_raw", _apply_replacements(self.get_state("last_md_raw")))
    
    
    def add_placeholder_to_draft(self, screenshot_name: str):
        """Add image placeholder to draft."""
        self.close_all_modals()
        current_draft = self.get_state("draft")
        if current_draft and not current_draft.endswith("\n"):
            current_draft += "\n"
        
        new_draft = current_draft + f"![Short description]({screenshot_name})\n"
        self.set_state("draft", new_draft)
    
    def handle_file_upload(self, uploaded_files):
        """Process uploaded files and update screenshots list."""
        if not uploaded_files:
            return
            
        temp_dir = tempfile.gettempdir()
        screenshots = self.get_state("shots")
        initial_count = len(screenshots)
        
        # Prevent duplicates based on original filename
        existing_files = {shot.get("orig") for shot in screenshots}
        
        for uploaded_file in uploaded_files:
            if uploaded_file.name in existing_files:
                continue
                
            temp_path = save_uploaded_file(uploaded_file, temp_dir)
            screenshots.append({
                "name": "",
                "path": temp_path,
                "orig": uploaded_file.name
            })
        
        # Update state if new files were added
        if len(screenshots) != initial_count:
            self.renumber_screenshots()
            self.set_state("shots", screenshots)
            st.session_state.app1_sort_ver += 1
        
        # Reset file uploader
        st.session_state.app1_upload_ver += 1
        st.rerun()
    
    def handle_screenshot_reorder(self, screenshot_names):
        """Handle drag-and-drop reordering of screenshots."""
        if not screenshot_names:
            return
            
        current_names = [shot["name"] for shot in self.get_state("shots")]
        
        if screenshot_names != current_names:
            # Reorder screenshots based on new order
            name_to_shot = {shot["name"]: shot for shot in self.get_state("shots")}
            reordered_shots = [name_to_shot[name] for name in screenshot_names]
            self.set_state("shots", reordered_shots)
            
            # Renumber sequentially (screenshot1, screenshot2, …) to keep stable placeholders
            self.renumber_screenshots()
            
            # Close all modals before rerun
            self.close_all_modals()
            
            st.session_state.app1_sort_ver += 1
            st.rerun()
    
    def create_markdown_with_images(self, base_markdown: str) -> str:
        """Create markdown with embedded base64 images."""
        result_markdown = base_markdown
        
        for shot in self.get_state("shots"):
            screenshot_name = shot["name"]
            mime_type = determine_image_mime_type(screenshot_name)
            base64_data = encode_image_to_base64(shot["path"])
            
            if base64_data:
                base64_url = f"data:image/{mime_type};base64,{base64_data}"
                result_markdown = result_markdown.replace(f"({screenshot_name})", f"({base64_url})")
        
        return result_markdown
    
    def _stream_response_markdown(self, response) -> tuple[str, str]:
        """Stream response: live updates via st.markdown, final render via render_markdown.
        During generation we show raw tokens immediately with the lightweight
        Streamlit markdown renderer. Once the stream completes we run the usual
        post-processing (merge line-breaks + embed base64 screenshots) and then
        pass the result to `render_markdown`, which uses the full-featured
        st_markdown component with copy-buttons etc.
        """
        placeholder = self.get_state("md_placeholder") or st.empty()
        self.set_state("md_placeholder", placeholder)

        raw_content = ""
        md_area = placeholder.empty()  # live render area for streaming text

        # Read streaming response chunk-by-chunk and update the UI
        for chunk in response.iter_content(chunk_size=1024, decode_unicode=True):
            if not chunk:
                continue
            raw_content += chunk
            md_area.markdown(raw_content, unsafe_allow_html=True)

        # After streaming finishes – run post-processing
        processed = fix_markdown_linebreaks(raw_content)
        final_md = self.create_markdown_with_images(processed)

        # Replace the live area with the final rich markdown
        placeholder.empty()
        render_markdown(placeholder, final_md)
        return processed, final_md

    def stream_generate_killchain(self):
        """Generate killchain report using streaming API."""
        if self.get_state("busy"):
            return

        self.set_state("busy", True)
        self.close_all_modals()

        user_message = f"# {self.get_state('title')}\n\n{self.get_state('draft')}"
        self.set_state("history", [{"role": "user", "content": user_message}])

        payload = {
            "history": self.get_state("history"),
            "images": [shot["path"] for shot in self.get_state("shots")],
            "filenames": [shot["name"] for shot in self.get_state("shots")],
        }

        try:
            resp = requests.post(
                f"{API_LL}/generate/killchain/stream",
                json=payload,
                stream=True,
                timeout=180,
            )
            resp.raise_for_status()
        except Exception as e:
            st.error(f"Generation error: {e}")
            self.set_state("busy", False)
            return

        raw, final = self._stream_response_markdown(resp)
        self.set_state("last_md_raw", raw)
        self.set_state("last_md", final)
        self.set_state("history", self.get_state("history") + [{"role": "assistant", "content": raw}])
        self.set_state("busy", False)

    def stream_generate_report(self):
        """Generate report using streaming API."""
        if self.get_state("busy"):
            return

        self.set_state("busy", True)
        self.close_all_modals()

        user_message = f"# {self.get_state('title')}\n\n{self.get_state('draft')}"
        self.set_state("history", [{"role": "user", "content": user_message}])

        payload = {
            "history": self.get_state("history"),
            "images": [shot["path"] for shot in self.get_state("shots")],
            "filenames": [shot["name"] for shot in self.get_state("shots")],
        }

        try:
            resp = requests.post(
                f"{API_LL}/generate/stream",
                json=payload,
                stream=True,
                timeout=180,
            )
            resp.raise_for_status()
        except Exception as e:
            st.error(f"Generation error: {e}")
            self.set_state("busy", False)
            return

        raw, final = self._stream_response_markdown(resp)
        self.set_state("last_md_raw", raw)
        self.set_state("last_md", final)
        self.set_state("history", self.get_state("history") + [{"role": "assistant", "content": raw}])
        self.set_state("busy", False)

    def follow_up_generate(self, follow_up_text: str):
        """Generate follow-up response with full context."""
        if self.get_state("busy"):
            return
        
        if not self.get_state("last_md_raw"):
            st.warning("Generate the report first, then refine 🙃")
            return
        
        self.set_state("busy", True)
        self.close_all_modals()
        
        # Include existing report in context
        full_message = (
            follow_up_text.strip() +
            "\n\n---\n\n" +
            "Current report in Markdown (for context):\n\n" +
            self.get_state("last_md_raw")
        )
        
        updated_history = self.get_state("history") + [{"role": "user", "content": full_message}]
        self.set_state("history", updated_history)
        
        payload = {
            "history": updated_history,
            "images": [shot["path"] for shot in self.get_state("shots")],
            "filenames": [shot["name"] for shot in self.get_state("shots")],
        }
        
        try:
            resp = requests.post(
                f"{API_LL}/generate/stream",
                json=payload,
                stream=True,
                timeout=180,
            )
            resp.raise_for_status()
        except Exception as e:
            st.error(f"Error while generating: {e}")
            self.set_state("busy", False)
            return
        
        raw, final = self._stream_response_markdown(resp)
        self.set_state("last_md_raw", raw)
        self.set_state("last_md", final)
        self.set_state("history", updated_history + [{"role": "assistant", "content": raw}])
        self.set_state("busy", False)
    
    def open_zoom_modal(self, image_path: str):
        """Open zoom modal for image."""
        self.close_all_modals()  # Close all modal windows first
        self.set_state("zoom_open", True)
        self.set_state("zoom_path", image_path)
    def _ocr_image(self, path: str) -> str:
        try:
            with open(path, "rb") as fp:
                payload = {"image": base64.b64encode(fp.read()).decode()}
            resp = requests.post(OCR_URL, json=payload, timeout=160)
            resp.raise_for_status()
            return resp.json().get("text", "")
        except Exception as e:
            st.error(f"OCR error: {e}")
            return ""

    def ocr_and_insert(self, path: str):
        # 1️⃣  First, close Zoom and other windows
        self.close_all_modals()

        # 2️⃣  OCR
        text = self._ocr_image(path)
        if not text:
            return

        buf = self.get_state("draft")
        if buf and not buf.endswith("\n"):
            buf += "\n"

        self.set_state("draft", buf + f"{text}\n")
        st.toast("OCR added", icon="🔤")

    def open_ocr_modal(self, image_path: str):
        """Open OCR modal for image."""
        # Close all modal windows first
        self.close_all_modals()
        self.set_state("ocr_modal_open", True)
        self.set_state("ocr_text", "")
        self.set_state("ocr_streaming", True)
        self.set_state("current_ocr_path", image_path)
        self.set_state("ocr_finished", False)
    
    def open_restore_modal(self):
        """Open restore modal with saved reports."""
        self.close_all_modals()  # Close all modal windows first
        reports = requests.get(f"{API_LL}/reports/default").json()
        restore_map = {
            f"{i+1}. {time.ctime(report['ts'])}": report["id"] 
            for i, report in enumerate(reports)
        }
        self.set_state("restore_map", restore_map)
        self.set_state("restore_open", True)
    
    def close_ocr_modal(self):
        """Close OCR modal."""
        self.set_state("ocr_modal_open", False)
        self.set_state("ocr_streaming", False)
        self.set_state("ocr_finished", False)
        self.set_state("ocr_text", "")
        self.set_state("current_ocr_path", None)
    
    def add_ocr_to_draft(self):
        """Add OCR text to draft."""
        ocr_text = self.get_state("ocr_text")
        if not ocr_text:
            return
            
        current_draft = self.get_state("draft")
        if current_draft and not current_draft.endswith("\n"):
            current_draft += "\n"
        
        self.set_state("draft", current_draft + ocr_text + "\n")
        st.toast("OCR текст добавлен", icon="🔤")
        self.close_ocr_modal()
    
    def stream_ocr_text(self, image_path: str):
        """Stream OCR text from image."""
        response = requests.get(
            f"{OCR_URL}/ocr/stream",
            params={"path": image_path},
            stream=True,
            timeout=(10, 150)
        )
        response.raise_for_status()
        
        buffer = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if raw_line == "":  # Event separator
                if buffer:
                    yield "".join(buffer)
                    buffer.clear()
                continue
            
            if raw_line.startswith("data:"):
                buffer.append(raw_line[5:].lstrip())  # Remove 'data:' prefix
        
        if buffer:  # Handle remaining buffer
            yield "".join(buffer)
    
    def save_report(self):
        """Save current report via API."""
        if not self.get_state("last_md"):
            return
            
        payload = {
            "markdown": self.get_state("last_md"),
            "images": [shot["path"] for shot in self.get_state("shots")],
            "filenames": [shot["name"] for shot in self.get_state("shots")],
            "history": self.get_state("history"),
        }
        
        requests.post(f"{API_LL}/reports/save", json=payload)

    def restore_report(self, report_id: str):
        """Fetch a saved report from the backend and load it into the current session state."""
        try:
            resp = requests.get(f"{API_LL}/reports/default/{report_id}", timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            st.error(f"Error loading report: {e}")
            return

        # Update state with restored data
        self.set_state("last_md", data.get("markdown", ""))
        self.set_state("last_md_raw", data.get("markdown", ""))
        self.set_state("history", data.get("history", []))
        # Clear current screenshots and draft since report already contains embedded images
        self.set_state("shots", [])
        self.set_state("draft", "")
        # Close restore modal
        self.set_state("restore_open", False)
        st.toast("Report has been restored.", icon="↩")
        st.rerun()
    
    def render_ui(self):
        """Render the vulnerability report generator UI."""
        left_col, right_col = st.columns([1, 1])
        
        with left_col:
            # Report type selector
            st.subheader("INPUT")
            report_type = st.radio(
                "Report type",
                ["VULNERABILITY", "KILLCHAIN"],
                horizontal=True,
                index=0 if self.get_state("report_type") == "VULNERABILITY" else 1,
                key="app1_report_type"
            )
            
            # Apply any pending draft update *before* widgets are instantiated

            st.markdown("""
            <style>
            .stTextInput input[aria-label="Vulnerability name"]{
            background-color:#F8F9FA !important;
            color:#000000 !important;
            border: 2px solid #FFFFFF !important;
            border-color:#E1E3E8 !important; 
            }

            /* Recolor autofill in WebKit/Chromium (Chrome/Safari/Edge) */
            .stTextInput input[aria-label="Vulnerability name"]:is(:-webkit-autofill, :autofill){
            -webkit-text-fill-color:#FFFFFF !important;
            box-shadow: 0 0 0 1000px #F8F9FA inset !important;
            -webkit-box-shadow: 0 0 0 1000px #262730 inset !important;
            caret-color:#FFFFFF;
            }
            </style>
            """, unsafe_allow_html=True)
            # Input fields
            st.text_input("Vulnerability name", key="app1_title")
            
            st.markdown("""
            <style>
            /* This text field only */
            .stTextArea textarea[aria-label="Stream of thoughts"]{
            background-color:#F8F9FA !important; 
            color:#000000 !important;  
            border: 2px solid #FFFFFF !important;
            border-color:#E1E3E8 !important;
            }


            </style>
            """, unsafe_allow_html=True)
            st.text_area("Stream of thoughts", key="app1_draft", height=220)
            st.subheader("FILES")
            # File uploader
            
            st.markdown("""
            <style>
            /* Dropzone box */
            [data-testid="stFileUploader"] section{
            background-color:#F8F9FA !important;
            color:#FAFAFA !important;
            border:2px solid #F9FAFC !important;
            border-color:#E1E3E8 !important;
            border-radius:8px;
            }

            /* Text inside the dropzone (hints "Drag & drop...", "Browse files") */
            [data-testid="stFileUploader"] section div{
            color:#FAFAFA !important;
            }

            /* Button inside uploader (if needed) */
            [data-testid="stFileUploader"] button{
            background:#3a3b44 !important;
            color:#FAFAFA !important;
            border-color:#3a3b44 !important;
            }
            </style>
            """, unsafe_allow_html=True)

            uploader_key = f"app1_uploader_{st.session_state.app1_upload_ver}"
            uploaded_files = st.file_uploader(
                "Screenshots",
                ["png", "jpg", "jpeg"],
                accept_multiple_files=True,
                key=uploader_key,
            )
            
            self.handle_file_upload(uploaded_files)
            
            # Screenshot sorting and preview
            screenshot_names = [shot["name"] for shot in self.get_state("shots")]
            if screenshot_names:
                st_markdown("Drag and drop screens in correct order", theme_color="gray", key="shots_order_label")
                sort_key = f"app1_sort_{st.session_state.app1_sort_ver}"
                new_order = sort_items(screenshot_names, key=sort_key) or screenshot_names
                self.handle_screenshot_reorder(new_order)
                
                # Screenshot preview grid
                columns = st.columns(len(self.get_state("shots")))
                for i, (col, shot) in enumerate(zip(columns, self.get_state("shots"))):
                    col.image(shot["path"], width=180)
                    col.button("🔍", key=f"app1_zoom{i}", 
                             on_click=self.open_zoom_modal, args=(shot["path"],), disabled=self.get_state("busy"))
                    col.button("✏️", key=f"app1_ph{i}", 
                             on_click=self.add_placeholder_to_draft, args=(shot["name"],), disabled=self.get_state("busy"))
                    col.button("🔤", key=f"app1_ocr{i}", 
                             on_click=self.ocr_and_insert, args=(shot["path"],), disabled=self.get_state("busy"))
            
            # Generate button chooses endpoint by report_type
            generate_handler = self.stream_generate_report if report_type == "VULNERABILITY" else self.stream_generate_killchain
            st.button(
                "▶️ Generate!",
                on_click=generate_handler,
                type="primary",
                disabled=self.get_state("busy"),
                use_container_width=True
            )
            
            # Follow-up chat
            if self.get_state("last_md"):
                follow_up = st.chat_input("Ask for something", key="app1_follow")
                if follow_up:
                    self.follow_up_generate(follow_up)
                    st.rerun()
            
            # (Removed action buttons from left column)
        
        with right_col:
            st.subheader("OUTPUT")
            # Create a fresh placeholder every run so the Markdown always stays visible
            markdown_placeholder = st.empty()
            self.set_state("md_placeholder", markdown_placeholder)
            if self.get_state("last_md"):
                render_markdown(markdown_placeholder, self.get_state("last_md"))

            # Action buttons moved here
            action_cols = st.columns(3)
            with action_cols[0]:
                st.button(
                    "Save draft",
                    on_click=self.save_report,
                    disabled=not bool(self.get_state("last_md")),
                    use_container_width=True
                )

            with action_cols[2]:
                st.download_button(
                    "💾 Download .md",
                    data=self.get_state("last_md"),
                    file_name=f"{(self.get_state('title') or 'report').replace(' ', '_')}.md",
                    mime="text/markdown",
                    disabled=not bool(self.get_state("last_md")),
                    key="app1_download",
                    use_container_width=True
                )

            with action_cols[1]:
                st.button("Restore draft", on_click=self.open_restore_modal, use_container_width=True)
        
        # Render modals - important: render only one open modal window
        self.render_modals()
    
    def render_modals(self):
        """Render modal dialogs. Only one modal can be open at a time."""
        # Do not show any modal windows while generation is in progress
        if self.get_state("busy"):
            return
        # We only check one modal window at a time
        if self.get_state("zoom_open"):
            self.render_zoom_modal()
        elif self.get_state("restore_open"):
            self.render_restore_modal()
        elif self.get_state("ocr_modal_open"):
            self.render_ocr_modal()
    
    @st.dialog("🔍 Zoomed view", width="large")
    def render_zoom_modal(self):
        """Render zoom modal for image preview."""
        zoom_path = self.get_state("zoom_path")
        if zoom_path:
            st.image(zoom_path, use_container_width=True)
        
        # Close button
        if st.button("Close"):
            self.set_state("zoom_open", False)
            self.set_state("zoom_path", None)
            st.rerun()
    
    @st.dialog("↩ Restore report", width="medium")
    def render_restore_modal(self):
        """Render restore modal for saved reports."""
        restore_map = self.get_state("restore_map")
        if restore_map:
            selected = st.selectbox("Select a report:", list(restore_map.keys()))
            
            col1, col2 = st.columns(2)
            
            with col1:
                if st.button("Restore"):
                    report_id = restore_map[selected]
                    # Updated: call restore_report to actually load the report
                    self.restore_report(report_id)
            
            with col2:
                if st.button("Cancel"):
                    self.set_state("restore_open", False)
                    st.rerun()
    
    @st.dialog("🔤 OCR", width="large")
    def render_ocr_modal(self):
        """Render OCR modal for text extraction."""
        if self.get_state("ocr_streaming"):
            status_placeholder = st.empty()
            text_placeholder = st.empty()
            
            status_placeholder.info("⏳ OCR processing...")
            
            accumulated_text = ""
            try:
                for line in self.stream_ocr_text(self.get_state("current_ocr_path")):
                    accumulated_text += line
                    render_markdown(text_placeholder, f"```text\n{accumulated_text}\n```")
                
                status_placeholder.success("✅ Ready")
                self.set_state("ocr_text", accumulated_text)
                self.set_state("ocr_streaming", False)
                self.set_state("ocr_finished", True)
                st.rerun()
            except Exception as e:
                status_placeholder.error(f"OCR Error: {e}")
                self.set_state("ocr_streaming", False)
        
        elif self.get_state("ocr_finished"):
            # Show OCR result
            st_markdown("**OCR Result:**", key="ocr_result_label")
            st_markdown(f"```text\n{self.get_state('ocr_text')}\n```", key="ocr_result_text")
            
            col1, col2 = st.columns(2)
            
            with col1:
                if st.button("Add to draft"):
                    self.add_ocr_to_draft()
                    st.rerun()
            
            with col2:
                if st.button("Close"):
                    self.close_ocr_modal()
                    st.rerun()

# ═══════════════════════════════════════════════════════════════════════════════
# MARKDOWN COMBINER
# ═══════════════════════════════════════════════════════════════════════════════

class MarkdownCombiner:
    """Handles markdown file combination and executive summary generation."""
    
    def __init__(self):
        self.session_keys = {
            "files": [],
            "active_id": None,
            "summary": "",
            "streaming": False,
            "stats": "",
        }
        self._initialize_session_state()
    
    def _initialize_session_state(self):
        """Initialize session state for markdown combiner."""
        for key, default_value in self.session_keys.items():
            session_key = f"app2_{key}"
            if session_key not in st.session_state:
                st.session_state[session_key] = default_value
    
    def add_files(self, uploaded_files):
        """Add uploaded markdown files to the list."""
        if not uploaded_files:
            return
            
        for file in uploaded_files:
            file_data = {
                "id": f"{file.name}_{len(st.session_state.app2_files)}",
                "name": file.name,
                "content": file.getvalue().decode("utf-8"),
            }
            st.session_state.app2_files.append(file_data)
        
        # Set active file if none selected
        if st.session_state.app2_active_id is None and st.session_state.app2_files:
            st.session_state.app2_active_id = st.session_state.app2_files[0]["id"]
    
    def get_merged_content(self, clean_images=False):
        """Get merged content from all files."""
        merged = "\n\n".join(file["content"] for file in st.session_state.app2_files)
        return clean_base64_images(merged) if clean_images else merged
    
    def stream_executive_summary(self, markdown_text: str):
        """Generate executive summary using Ollama streaming."""
        prompt = (
            "You are a cybersecurity expert preparing the Executive Summary section of an internal/external penetration test report. Your audience is executives and non-technical stakeholders. Use clear business English, avoid jargon without explanation. Below are the vulnerabilities found\n\n" + markdown_text
        )
        
        payload = {
            "model": "gemma3:27b",
            "prompt": prompt,
            "stream": True,
            "options": {
                "temperature": 0.2,
                "top_p": 0.9,
                "num_predict": 1024,
            },
        }
        
        with requests.post(f"{OLLAMA_URL}/api/generate", json=payload, stream=True, timeout=600) as response:
            response.raise_for_status()
            
            for raw_line in response.iter_lines(chunk_size=8192):
                if not raw_line:
                    continue
                
                # Decode bytes to string
                line = raw_line.decode() if isinstance(raw_line, (bytes, bytearray)) else raw_line
                
                # Skip comments and handle data lines
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                
                # Skip empty lines and completion markers
                if not line or line == "[DONE]":
                    continue
                
                try:
                    message = json.loads(line)
                    if message.get("done"):
                        break
                    yield message.get("response", "")
                except json.JSONDecodeError:
                    continue
    
    def generate_summary(self, summary_container):
        """Generate and stream executive summary (live via st.markdown, final via render_markdown)."""
        st.session_state.app2_summary = ""
        st.session_state.app2_streaming = True

        summary_container.empty()  # clear previous output
        clean_content = self.get_merged_content(clean_images=True)

        md_area = summary_container  # use the same placeholder for live updates
        with st.spinner("Generating Executive Summary…"):
            for token in self.stream_executive_summary(clean_content):
                st.session_state.app2_summary += token
                md_area.markdown(st.session_state.app2_summary, unsafe_allow_html=True)

        # streaming finished – flag so render_ui can replace with rich markdown
        st.session_state.app2_streaming = False

    # ──────────────────────────────────────────────────────────────────────
    # 📊 Vulnerability statistics generation
    # ──────────────────────────────────────────────────────────────────────

    def _parse_vuln_statistics(self, markdown_text: str) -> dict[str, list[str]]:
        """Extract vulnerabilities grouped by severity from *markdown_text*.

        Returns a mapping {severity: ["VULN-ID: title", ...]} for the four
        standard severities Critical, High, Medium and Low. Severities that are
        not present will have an empty list so downstream code can rely on all
        keys being available.
        """
        severities = {s: [] for s in ("Critical", "High", "Medium", "Low")}

        # Regex to capture a vulnerability header (## ID: title)
        header_re = re.compile(r"^##\s+(.+)$", re.MULTILINE)

        # Regex to capture severity inside the section – we look for either the
        # alt-text part of the badge ( ![CRITICAL] ) *or* the segment
        # "Severity-Critical" inside the shields.io URL.
        sev_re = re.compile(r"(?:!\[([A-Za-z]+)\]|Severity-([A-Za-z]+))", re.IGNORECASE)

        matches = list(header_re.finditer(markdown_text))
        for idx, m in enumerate(matches):
            vuln_title = m.group(1).strip()

            # Determine slice for current section (until next header or EOF)
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown_text)
            section = markdown_text[start:end]

            sev_match = sev_re.search(section)
            if not sev_match:
                continue  # severity not found – skip

            sev_raw = (sev_match.group(1) or sev_match.group(2) or "").capitalize()
            if sev_raw in severities:
                severities[sev_raw].append(vuln_title)

        return severities

    def generate_statistics(self, stats_container):
        """Generate statistics chapter and render it inside *stats_container*."""
        merged_md = self.get_merged_content(clean_images=True)
        stats_map = self._parse_vuln_statistics(merged_md)

        # Create count table
        counts = {s: len(stats_map[s]) for s in ("Critical", "High", "Medium", "Low")}
        table = (
            "| Critical | High | Medium | Low |\n"
            "| ------------ | ------- | ------- | ------ |\n"
            f"| {counts['Critical']} | {counts['High']} | {counts['Medium']} | {counts['Low']} |\n"
        )

        # Create grouped list with bullet points so Markdown keeps line breaks
        groups_md_lines = []
        for sev in ("Critical", "High", "Medium", "Low"):
            groups_md_lines.append(f"\n**{sev} - {counts[sev]}**")
            for item in stats_map[sev]:
                groups_md_lines.append(f"- {item}")

        stats_md = table + "\n" + "\n".join(groups_md_lines)

        # Persist and render
        st.session_state.app2_stats = stats_md
        render_markdown(stats_container, "## Vulnerability Statistics\n\n" + stats_md)
    
    def render_ui(self):
        """Render the markdown combiner UI."""
        # ──────────────────────────────────────────────────────────────────────
        # Two-column layout (left — file management, right — content)
        # ──────────────────────────────────────────────────────────────────────
        left_col, right_col = st.columns([1, 1], gap="medium")
        summary_container = right_col.empty()

        # LEFT column — file upload and management
        with left_col:
            # File uploader
            st.subheader("INPUT")
            uploaded_files = st.file_uploader(
                "Markdown",
                type=["md"],
                accept_multiple_files=True,
                key="app2_uploader"
            )
            st.subheader("FILES")
            # Add uploaded files to session
            self.add_files(uploaded_files)

            # If no files — show a hint and exit earlier,
            # so the right column remains empty.
            if not st.session_state.app2_files:
                st.info("Upload your .md files")
                return
            
            # Draggable file list
            reordered_files = DraggableList(
                st.session_state.app2_files,
                key="app2_dlist",
                style={
                    "item": {
                        "padding": "8px 12px",
                        "margin": "4px 0",
                        "borderRadius": "8px",
                        "background": "var(--secondary-background)",
                        "cursor": "grab",
                    }
                },
            )
            
            if isinstance(reordered_files, list):
                st.session_state.app2_files = reordered_files
            
            # File selector
            file_names = [file["name"] for file in st.session_state.app2_files]
            current_index = next(
                (i for i, f in enumerate(st.session_state.app2_files) 
                 if f["id"] == st.session_state.app2_active_id), 
                0
            )
            
            selected_name = st.radio(
                "Preview:",
                file_names,
                index=current_index,
                label_visibility="collapsed",
                key="app2_radio"
            )
            
            # Update active file
            st.session_state.app2_active_id = next(
                file["id"] for file in st.session_state.app2_files 
                if file["name"] == selected_name
            )
            
            # Export buttons
            raw_content = self.get_merged_content()
            clean_content = self.get_merged_content(clean_images=True)
            
            
            st.subheader("ACTIONS")
            # Generate summary button
            if st.button("📝 Write Executive summary", key="app2_summary_btn"):
                self.generate_summary(summary_container)

            # Generate statistics button
            if st.button("📊 Write Statistic Chapter", key="app2_stats_btn"):
                self.generate_statistics(summary_container)
            st.download_button(
                "🔀 Merge files",
                raw_content,
                file_name="combined.md",
                mime="text/markdown",
                key="app2_export_raw")


        # ──────────────────────────────────────────────────────────────────────
        # RIGHT column — view and download content
        # ──────────────────────────────────────────────────────────────────────
        with right_col:
            if st.session_state.app2_streaming:
                # During the generation of the Executive Summary, the text is gradually streamed into the summary_container, no additional actions are required.
                pass
            elif st.session_state.app2_stats:
                # Display Statistics chapter
                render_markdown(
                    summary_container,
                    "## Vulnerability Statistics\n\n" + st.session_state.app2_stats,
                )
                st.download_button(
                    "💾 Download .md",
                    data="## Vulnerability Statistics\n\n" + st.session_state.app2_stats,
                    file_name="statistics.md",
                    mime="text/markdown",
                    key="app2_download_stats",
                )
            elif st.session_state.app2_summary:
                # We show the finished Executive Summary
                render_markdown(
                    summary_container,
                    "## Executive Summary\n\n" + st.session_state.app2_summary,
                )
                st.download_button(
                    "💾 Download .md",
                    data="## Executive Summary\n\n" + st.session_state.app2_summary,
                    file_name="executive_summary.md",
                    mime="text/markdown",
                    key="app2_download_summary",
                )
            else:
                # Preview active file
                active_file = next(
                    file for file in st.session_state.app2_files
                    if file["id"] == st.session_state.app2_active_id
                )
                st.subheader(active_file["name"])

                st_markdown(
                    active_file["content"],
                    theme_color="gray",
                    unsafe_allow_html=True,
                    key="active_file_preview",
                )
                st.download_button(
                    "💾 Download .md",
                    data=active_file["content"],
                    file_name=active_file["name"],
                    mime="text/markdown",
                    key="app2_download_active",
                )

def main():
    """Main application entry point."""


    # Custom Header
    logo_col, user_col = st.columns([0.87, 0.13])
    with logo_col:
        st.markdown("""
        <style>
        .app-logo {
            display:inline-block;
            transform: scale(0.6);         
            transform-origin: top left;     /* scale from top left corner */
        }
        </style>
        """, unsafe_allow_html=True)

        with logo_col:
            st.markdown('<img class="app-logo" src="/app/static/logo.png" alt="logo">', unsafe_allow_html=True)
    with user_col:
        st.markdown(
            '<img src="/app/static/logo2.gif" style="height:64px;">',
            unsafe_allow_html=True
        )




    st.write("") # Spacer
    
    # Create tabs
    app1_tab, app2_tab = st.tabs(["**REPORT GENERATOR**", "**MARKDOWN COMBINER**"])
    
    # Initialize applications
    vuln_generator = VulnReportGenerator()
    markdown_combiner = MarkdownCombiner()
    
    # Render applications in their respective tabs
    with app1_tab:
        vuln_generator.render_ui()
    
    with app2_tab:
        markdown_combiner.render_ui()

if __name__ == "__main__":
    main()
