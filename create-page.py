import json
import os
import subprocess
import argparse
import sys
from pathlib import Path
import shlex
import tempfile
import math

# --- Configuration ---
# Page Dimensions
PAGE_W = 1368.0
PAGE_H = 936.0
PDFCPU_PAPER_NAME = "SuperBL"

# Layout Configuration
TEXT_X_OFFSET = 10.0       # Offset for image labels from image left
TEXT_Y_OFFSET = -20.0      # Offset for image labels from image bottom (negative is above)
MIN_TEXT_Y = 10.0         # Minimum Y coord for image labels
PAGE_TITLE_FONT = "Helvetica-Bold"
PAGE_TITLE_SIZE = 45
PAGE_TITLE_COLOR = "#000000" # Black
IMAGE_LABEL_SIZE = 16
TITLE_ASCENT_FACTOR = 0.8 # Approx. ascent relative to font size for title top margin calc

# Rotated Copyright Text Configuration
COPYRIGHT_TEXT = "  \u00A9 Thomas Hollier, Relentless Play" # \u00A9 is ©
COPYRIGHT_FONT = "Helvetica"
COPYRIGHT_SIZE = 14
COPYRIGHT_COLOR = "#aaaaaa" # 50% Grey
COPYRIGHT_ROTATION = 0 # Degrees CCW
COPYRIGHT_ANCHOR = "bottomleft" # Anchor point: middle-right of unrotated text box
COPYRIGHT_MARGIN_RIGHT = 15 # Points inward from right page edge
COPYRIGHT_POS_Y = 10 # Points up from bottom page edge for the anchor point

# ImageMagick Parameters
MAGICK_BORDER_COLOR_INNER = "#cccccc"
MAGICK_BORDER_INNER_SIZE = "2x2"
MAGICK_SHADOW_COLOR = "#404040"
MAGICK_SHADOW_GEOMETRY = "33x20+15+15"
MAGICK_BACKGROUND_MERGE = "white"
PROCESSED_SUFFIX = "_processed"
PROCESSED_IMG_FORMAT = ".png"

# Exiftool Tags
EXIFTOOL_TITLE_TAGS = [
    "ObjectName", "Headline", "Title", "ImageDescription",
    "Description", "UserComment", "Label"
]

# --- Helper Functions ---

def run_command(cmd_parts, description, cwd=None):
    """Runs a command using subprocess and handles errors."""
    cmd_str_for_print = " ".join(shlex.quote(str(part)) for part in cmd_parts)
    print(f"Running {description}: {cmd_str_for_print}")
    try:
        process = subprocess.Popen(
            cmd_parts, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', cwd=cwd
        )
        stdout, stderr = process.communicate()
        log_prefix = description.split()[0].replace('`','')
        stdout_stripped = stdout.strip() if stdout else ""
        stderr_stripped = stderr.strip() if stderr else ""
        if stdout_stripped: print(f"--- {log_prefix} stdout ---\n{stdout_stripped}\n---------------------")
        if stderr_stripped: print(f"--- {log_prefix} stderr ---\n{stderr_stripped}\n---------------------",
                                  file=sys.stderr if process.returncode not in [0, 1] else sys.stdout)
        if process.returncode not in [0, 1]:
            print(f"Error running {description}. Return code: {process.returncode}", file=sys.stderr)
            return False, None, None
        if process.returncode == 1 and not stdout_stripped and "Warning: Error opening file" not in stderr_stripped and "Error: File not found" not in stderr_stripped:
             print(f"Warning/Error running {description}. RC 1 but no std output & no clear file error.", file=sys.stderr)
        print(f"{description} completed (RC={process.returncode}).")
        return True, stdout, stderr
    except FileNotFoundError: print(f"Error: Command '{cmd_parts[0]}' not found.", file=sys.stderr); return False, None, None
    except Exception as e: print(f"An unexpected error occurred running {description}: {e}", file=sys.stderr); print(f"Failed Command: {cmd_str_for_print}", file=sys.stderr); return False, None, None

def get_image_info_exiftool(image_path):
    """Gets the width, height, and title metadata of an image using exiftool."""
    fallback_title = image_path.name
    tags_to_extract = ["-j", "-q", "-n", "-ImageWidth", "-ImageHeight", "-BaseDimensions"]
    tags_to_extract.extend([f"-{tag}" for tag in EXIFTOOL_TITLE_TAGS])
    cmd = ["exiftool"] + tags_to_extract + [str(image_path)]
    success, stdout, stderr = run_command(cmd, f"exiftool info for {image_path.name}")
    if not success or not stdout: print(f"Warning/Error: Failed exiftool info for {image_path.name}.", file=sys.stderr); return None
    try:
        json_output = stdout.strip(); json_start = json_output.find('[');
        if json_start == -1: raise json.JSONDecodeError("JSON start '[' not found", json_output, 0)
        json_data = json_output[json_start:]; data_list = json.loads(json_data)
        if not data_list: raise ValueError("Exiftool returned empty JSON list")
        data = data_list[0]; width = data.get("ImageWidth"); height = data.get("ImageHeight")
        if width is None or height is None or not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
             base_dims = data.get("BaseDimensions")
             if isinstance(base_dims, str) and 'x' in base_dims:
                 try: w_str, h_str = base_dims.split('x', 1); width = int(w_str); height = int(h_str); assert width>0 and height>0; print(f"  Using BaseDimensions: {width}x{height}")
                 except: print(f"Error parsing BaseDimensions '{base_dims}'", file=sys.stderr); return None
             else: print(f"Error: Missing/invalid dimensions. Data: {data}", file=sys.stderr); return None
        print(f"  Exiftool identified dimensions: {width} x {height}")
        title = fallback_title
        for tag in EXIFTOOL_TITLE_TAGS:
            potential_title = data.get(tag)
            if isinstance(potential_title, str) and potential_title.strip(): title = potential_title.strip(); print(f"  Found title metadata (tag: {tag}): '{title}'"); break
        else: print(f"  No suitable title metadata found. Using filename.")
        return width, height, title
    except json.JSONDecodeError as e: print(f"Error decoding JSON: {e}\nOutput: {stdout[:500]}...", file=sys.stderr); return None
    except Exception as e: print(f"Error processing exiftool data: {e}", file=sys.stderr); return None

def preprocess_image(input_path, output_path):
    """Runs the specified ImageMagick command on the input image."""
    cmd_parts = [ "magick", str(input_path), "-bordercolor", MAGICK_BORDER_COLOR_INNER, "-border", MAGICK_BORDER_INNER_SIZE, "(", "+clone", "-background", MAGICK_SHADOW_COLOR, "-shadow", MAGICK_SHADOW_GEOMETRY, ")", "+swap", "-background", MAGICK_BACKGROUND_MERGE, "-layers", "merge", "+repage", str(output_path) ]
    success, _, _ = run_command(cmd_parts, f"magick pre-processing for {input_path.name}")
    return success

# --- Main Processing Function ---

def process_images_and_create_pdf(
    template_json_path, image_files, output_pdf_path,
    target_width_perc, gap_perc, v_offset_perc, page_title,
    title_top_margin_perc,
    save_json_path=None):
    """
    Gets image info, pre-processes, calculates layout, adds titles, updates JSON, creates PDF.
    """
    output_dir_path = output_pdf_path.parent
    processed_image_paths_to_cleanup = []
    json_temp_file = None
    json_write_path = None
    pdf_created_successfully = False

    try: # Main try block

        # --- 1. Get Image Info & Pre-process ---
        print("\n--- Getting Image Info (exiftool) & Pre-processing ---")
        processed_images_info = []; total_aspect_ratio = 0.0; valid_images_count = 0
        for img_path_str in image_files:
            input_img_path = Path(img_path_str).resolve()
            if not input_img_path.is_file(): print(f"Error: Input file not found: {img_path_str}", file=sys.stderr); return False
            print(f"\nGetting info for: {input_img_path.name}")
            image_info = get_image_info_exiftool(input_img_path)
            if image_info is None: print(f"Failed info for {input_img_path.name}. Aborting.", file=sys.stderr); return False
            width, height, display_label = image_info
            processed_filename = f"{input_img_path.stem}{PROCESSED_SUFFIX}{PROCESSED_IMG_FORMAT}"
            output_processed_path = output_dir_path / processed_filename
            print(f"Processing: {input_img_path.name} -> {output_processed_path.name}")
            if not preprocess_image(input_img_path, output_processed_path): print(f"Failed pre-process {input_img_path.name}. Aborting.", file=sys.stderr); return False
            processed_image_paths_to_cleanup.append(output_processed_path)
            aspect_ratio = float(width) / float(height) if height > 0 else 1.0
            total_aspect_ratio += aspect_ratio; valid_images_count += 1
            processed_images_info.append({ "original_name": input_img_path.name, "display_label": display_label, "processed_path": output_processed_path, "original_width": width, "original_height": height, "original_aspect_ratio": aspect_ratio })
        if valid_images_count != 4: print(f"Error: Expected 4 valid images, got {valid_images_count}.", file=sys.stderr); return False
        avg_aspect_ratio = total_aspect_ratio / valid_images_count
        print(f"\nAverage original aspect ratio (from exiftool): {avg_aspect_ratio:.4f}")

        # --- 2. Calculate Layout Dimensions and Positions ---
        print("\n--- Calculating Layout Dimensions and Positions ---")
        try:
            total_block_w = PAGE_W * target_width_perc; image_w = total_block_w / (2.0 + gap_perc); gap_w = image_w * gap_perc;
            image_h = image_w / avg_aspect_ratio; gap_h = gap_w; total_block_h = 2 * image_h + gap_h;
            vertical_offset_points = PAGE_H * v_offset_perc
            block_start_x = max(0, (PAGE_W - total_block_w) / 2.0)
            block_start_y = max(0, (PAGE_H - total_block_h) / 2.0 - vertical_offset_points)
            desired_top_y = PAGE_H * (1.0 - title_top_margin_perc)
            estimated_ascent = PAGE_TITLE_SIZE * TITLE_ASCENT_FACTOR
            page_title_y_pos = desired_top_y - estimated_ascent
            page_title_y_pos = max(PAGE_TITLE_SIZE * (1.0 - TITLE_ASCENT_FACTOR), page_title_y_pos)
            pos = [ [block_start_x, block_start_y], [block_start_x + image_w + gap_w, block_start_y], [block_start_x, block_start_y + image_h + gap_h], [block_start_x + image_w + gap_w, block_start_y + image_h + gap_h] ]
            text_pos = [[max(0, p[0] + TEXT_X_OFFSET), max(MIN_TEXT_Y, p[1] + TEXT_Y_OFFSET)] for p in pos]

            # Calculate position for rotated copyright text
            copyright_pos_x = PAGE_W - COPYRIGHT_MARGIN_RIGHT
            copyright_pos_y = COPYRIGHT_POS_Y # Use constant for simplicity

            print(f"  Layout calculations based on page size: {PAGE_W} x {PAGE_H}")
            # ... (other print statements) ...
            print(f"  Page Title Top Margin: {title_top_margin_perc*100:.1f}% -> Desired Top Y: {desired_top_y:.2f}")
            print(f"  Approximated Page Title Baseline Y: {page_title_y_pos:.2f}")
            print(f"  Copyright Text Position (anchor={COPYRIGHT_ANCHOR}): ({copyright_pos_x:.2f}, {copyright_pos_y:.2f})")

        except Exception as e: print(f"Error during layout calculation: {e}", file=sys.stderr); return False

        # --- 3. Load Template JSON ---
        print(f"\n--- Loading template JSON structure from: {template_json_path} ---")
        try:
            with open(template_json_path, 'r', encoding='utf-8') as f: layout_data = json.load(f)
            layout_data["paper"] = PDFCPU_PAPER_NAME; print(f"  Set paper size to: \"{PDFCPU_PAPER_NAME}\"")
            pages = layout_data.setdefault("pages", {}); page1 = pages.setdefault("1", {}); content = page1.setdefault("content", {})
            content.setdefault("text", []); content.setdefault("image", [])
            if not isinstance(content["text"], list): print(f"Warning: Resetting non-list 'text'.", file=sys.stderr); content["text"] = []
            if not isinstance(content["image"], list): print(f"Warning: Resetting non-list 'image'.", file=sys.stderr); content["image"] = []
        except Exception as e: print(f"Error reading/parsing template JSON {template_json_path}: {e}", file=sys.stderr); return False

        # --- 4. Validate and Update JSON Data ---
        print("\n--- Updating JSON data with calculated layout and titles ---")
        try:
            page_content = layout_data["pages"]["1"]["content"]
            image_slots = page_content["image"]; text_slots = page_content["text"]
            if len(image_slots) < 4: image_slots.extend([{} for _ in range(4 - len(image_slots))])
            text_slots.clear() # Rebuild text list

            # Add Image Labels
            for i in range(4):
                processed_filename = processed_images_info[i]["processed_path"].name
                display_label = processed_images_info[i]["display_label"]
                image_slots[i].update({ "src": processed_filename, "width": round(image_w, 2), "height": round(image_h, 2), "pos": [round(coord, 2) for coord in pos[i]], "scale": image_slots[i].get("scale", "fit") })
                print(f"  Updated image slot {i}: src={processed_filename}, w/h/pos updated, scale={image_slots[i]['scale']}")
                text_slots.append({ "value": display_label, "pos": [round(coord, 2) for coord in text_pos[i]], "font": {"name": "Helvetica", "size": IMAGE_LABEL_SIZE, "col": "#333333"} })
                print(f"  Added text slot {i}: value='{display_label}', pos calculated, size={IMAGE_LABEL_SIZE}")

            # Add Page Title Text Element
            if page_title:
                title_x_pos = PAGE_W / 2.0
                page_title_element = { "value": page_title, "pos": [round(title_x_pos, 2), round(page_title_y_pos, 2)], "font": { "name": PAGE_TITLE_FONT, "size": PAGE_TITLE_SIZE, "col": PAGE_TITLE_COLOR }, "align": "center" }
                text_slots.append(page_title_element)
                print(f"  Added page title: '{page_title}' at baseline Y={page_title_element['pos'][1]:.2f} size={PAGE_TITLE_SIZE}")

            # *** Add Rotated Copyright Text Element ***
            copyright_element = {
                "value": COPYRIGHT_TEXT,
                #"pos": [round(copyright_pos_x, 2), round(copyright_pos_y, 2)],
                #"pos": [round(PAGE_W-5), round(110)],
                "font": {
                    "name": COPYRIGHT_FONT,
                    "size": COPYRIGHT_SIZE,
                    "col": COPYRIGHT_COLOR
                },
                "rot": COPYRIGHT_ROTATION,
                "anchor": COPYRIGHT_ANCHOR,
                "align":'left'
	# "align" is likely not needed with anchor="mr"
            }
            text_slots.append(copyright_element)
            print(f"  Added copyright text: '{COPYRIGHT_TEXT}' rot={COPYRIGHT_ROTATION}, anchor={COPYRIGHT_ANCHOR}")


        except Exception as e: print(f"Error updating JSON data: {e}", file=sys.stderr); return False

        # --- 5. Write Updated JSON File ---
        try:
            if save_json_path:
                json_write_path = save_json_path.resolve(); print(f"\n--- Saving updated layout JSON to: {json_write_path} ---")
                json_write_path.parent.mkdir(parents=True, exist_ok=True);
                with open(json_write_path, 'w', encoding='utf-8') as f: json.dump(layout_data, f, indent=2)
            else:
                output_dir_path.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', suffix=".json", prefix="temp_layout_", dir=output_dir_path, delete=False, encoding='utf-8') as tf:
                    json_write_path = Path(tf.name)
                    print(f"\n--- Saving updated layout JSON temporarily to: {json_write_path} ---")
                    json.dump(layout_data, tf)
                json_temp_file = json_write_path
        except Exception as e: print(f"Error writing JSON file: {e}", file=sys.stderr); return False


        # --- 6. Run pdfcpu create ---
        print(f"\n--- Creating PDF with `pdfcpu create {json_write_path.name} {output_pdf_path.name}` ---")
        pdfcpu_command = ["pdfcpu", "create", str(json_write_path), str(output_pdf_path.resolve())]
        success, _, _ = run_command(pdfcpu_command, "`pdfcpu create`", cwd=output_dir_path)
        if success:
            print(f"\n`pdfcpu create` completed using JSON. Output PDF: {output_pdf_path}")
            pdf_created_successfully = True
            return True
        else:
            print(f"\n`pdfcpu create` command failed.", file=sys.stderr)
            return False

    except Exception as e:
        print(f"An unexpected error occurred in main processing block: {e}", file=sys.stderr)
        return False

    finally:
        # --- 7. Clean up Temporary Files ---
        print("\n--- Cleaning up temporary files ---")
        if json_temp_file and not save_json_path:
            try:
                if json_temp_file.exists(): print(f"Deleting temporary JSON file: {json_temp_file}"); os.remove(json_temp_file)
                else: print(f"Temporary JSON file {json_temp_file} seems already gone.")
            except OSError as e: print(f"Warning: Could not delete temporary JSON file {json_temp_file}: {e}", file=sys.stderr)
        if pdf_created_successfully:
            print("Cleaning up processed image files...")
            for img_path in processed_image_paths_to_cleanup:
                try:
                    if img_path.exists(): print(f"Deleting processed image: {img_path.name}"); os.remove(img_path)
                    else: print(f"Processed image file {img_path.name} seems already gone.")
                except OSError as e: print(f"Warning: Could not delete processed image file {img_path}: {e}", file=sys.stderr)
        else: print("Skipping processed image cleanup because PDF creation failed or was aborted.")


# --- Main execution block ---
def main():
    parser = argparse.ArgumentParser(description="Get image title metadata, pre-process, calculate layout, add page title, update JSON, create PDF, cleanup.")
    parser.add_argument("template_json", help="Path to template JSON (used for structure).")
    parser.add_argument("image_files", nargs=4, help="Paths to exactly 4 input image files.")
    parser.add_argument("-o", "--output-pdf", required=True, help="Path for the output PDF file.")
    parser.add_argument("--save-json", help="Optional: Path to save the intermediate updated JSON file.")
    parser.add_argument("--target-width", type=float, default=0.75, help="Image block width percentage. Default: 0.75")
    parser.add_argument("--gap-percent", type=float, default=0.1, help="Gap percentage of image width. Default: 0.1")
    parser.add_argument("--v-offset-percent", type=float, default=0.05, help="Vertical offset percentage (+ shifts down). Default: 0.05")
    parser.add_argument("--page-title", type=str, default="Title", help="Main title text. Default: 'Title'")
    parser.add_argument("--title-top-margin-percent", type=float, default=0.08, help="Position TOP of title this %% down from page top. Default: 0.08")
    args = parser.parse_args()

    if not 0.0 < args.target_width <= 1.0: print("Error: --target-width must be > 0.0 and <= 1.0", file=sys.stderr); sys.exit(1)
    if not 0.0 <= args.gap_percent < 1.0: print("Error: --gap-percent must be >= 0.0 and < 1.0", file=sys.stderr); sys.exit(1)
    if not 0.0 <= args.title_top_margin_percent < 1.0: print("Error: --title-top-margin-percent must be >= 0.0 and < 1.0", file=sys.stderr); sys.exit(1)

    template_json_path = Path(args.template_json).resolve(); output_pdf_path = Path(args.output_pdf).resolve()
    save_json_path = Path(args.save_json).resolve() if args.save_json else None
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if save_json_path: save_json_path.parent.mkdir(parents=True, exist_ok=True)
    if not template_json_path.is_file(): print(f"Error: Template JSON not found: {args.template_json}", file=sys.stderr); sys.exit(1)

    if process_images_and_create_pdf(
        template_json_path, args.image_files, output_pdf_path,
        args.target_width, args.gap_percent, args.v_offset_percent, args.page_title,
        args.title_top_margin_percent,
        save_json_path):
        print("\nScript finished successfully.")
    else:
        print("\nScript finished with errors.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
