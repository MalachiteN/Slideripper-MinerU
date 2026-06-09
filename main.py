#!/usr/bin/env python3
"""
MinerU Precision API batch processor.
Flow: POST /api/v4/file-urls/batch → PUT files → poll → download zips

Usage: python precision_batch.py <batch_dir>
  batch_dir should have input/ subdirectory with PDFs.
  Results land in output/<filename>/.
"""
import os, sys, time, json, zipfile, requests

BATCH_DIR = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
INPUT_DIR = os.path.join(BATCH_DIR, "input")
OUTPUT_DIR = os.path.join(BATCH_DIR, "output")

# Read token from dedicated file (kept out of config.yaml to avoid leaking into context)
TOKEN_FILE = os.path.expanduser("./mineru_token")
try:
    with open(TOKEN_FILE) as f:
        TOKEN = f.read().strip()
except FileNotFoundError:
    print(f"ERROR: Token file not found at {TOKEN_FILE}")
    sys.exit(1)
if not TOKEN:
    print(f"ERROR: Token file {TOKEN_FILE} is empty")
    sys.exit(1)

BASE = "https://mineru.net"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {TOKEN}"
}

# --- Unpack any zip files in input/ ---
for f in sorted(os.listdir(INPUT_DIR)):
    if f.lower().endswith('.zip'):
        zip_path = os.path.join(INPUT_DIR, f)
        print(f"Unpacking: {f}")
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(INPUT_DIR)
        os.remove(zip_path)
        print(f"  → extracted and removed zip")

# --- Collect PDFs ---
FILES = []
for f in sorted(os.listdir(INPUT_DIR)):
    ext = f.lower().rsplit('.', 1)[-1] if '.' in f else ''
    if ext in ('pdf', 'doc', 'docx', 'ppt', 'pptx', 'png', 'jpg', 'jpeg', 'jp2', 'webp', 'gif', 'bmp'):
        FILES.append(f)

if not FILES:
    print("No supported files found in input/")
    sys.exit(1)

print(f"\nFiles to process ({len(FILES)}):")
for f in FILES:
    print(f"  - {f}")

# --- Step 1: Request upload URLs ---
print("\n[1/4] Requesting upload URLs...")
resp = requests.post(
    f"{BASE}/api/v4/file-urls/batch",
    headers=HEADERS,
    json={
        "files": [{"name": f} for f in FILES],
        "model_version": "vlm",
        "enable_formula": True,
        "enable_table": True,
        "language": "en"
    }
)
result = resp.json()
if result.get("code") != 0:
    print(f"ERROR: {result}")
    sys.exit(1)

batch_id = result["data"]["batch_id"]
urls = result["data"]["file_urls"]
print(f"batch_id: {batch_id}")
print(f"Got {len(urls)} upload URLs")

# --- Step 2: Upload files ---
print("\n[2/4] Uploading files...")
for i, (fname, url) in enumerate(zip(FILES, urls)):
    fpath = os.path.join(INPUT_DIR, fname)
    fsize = os.path.getsize(fpath)
    print(f"  [{i+1}/{len(FILES)}] {fname} ({fsize/1024:.0f} KB) ...", end=" ", flush=True)
    with open(fpath, 'rb') as f:
        up = requests.put(url, data=f)
    print(f"HTTP {up.status_code}")

# --- Step 3: Poll for results ---
print(f"\n[3/4] Polling results...")
poll_url = f"{BASE}/api/v4/extract-results/batch/{batch_id}"
done = set()

for attempt in range(60):  # up to 3 minutes
    time.sleep(3)
    poll = requests.get(poll_url, headers=HEADERS).json()
    extract_results = poll.get("data", {}).get("extract_result", [])
    
    statuses = []
    for er in extract_results:
        fname = er.get("file_name", "?")
        state = er.get("state", "?")
        statuses.append(f"{fname}:{state}")
        if state == "done" and fname not in done:
            done.add(fname)
            zip_url = er.get("full_zip_url", "")
            data_id = er.get("data_id", "?")
            
            # Download and extract
            subdir = os.path.join(OUTPUT_DIR, os.path.splitext(fname)[0])
            os.makedirs(subdir, exist_ok=True)
            
            zip_resp = requests.get(zip_url)
            tmp_zip = os.path.join(subdir, "_result.zip")
            with open(tmp_zip, 'wb') as zf:
                zf.write(zip_resp.content)
            with zipfile.ZipFile(tmp_zip, 'r') as zf:
                zf.extractall(subdir)
            os.remove(tmp_zip)
            
            img_count = len([
                x for x in os.listdir(os.path.join(subdir, "images"))
                if os.path.isdir(os.path.join(subdir, "images"))
            ]) if os.path.isdir(os.path.join(subdir, "images")) else \
                len([x for x in os.listdir(subdir) if x.startswith("images")])
            
            print(f"  ✓ {fname} → {subdir}/")
    
    if attempt % 5 == 0 or len(done) > 0:
        print(f"  [{attempt+1}] {' | '.join(statuses)}")
    
    if len(done) == len(FILES):
        break
    if all(er.get("state") in ("failed", "done") for er in extract_results):
        # Check for failures
        for er in extract_results:
            if er.get("state") == "failed":
                print(f"  ✗ {er.get('file_name')}: {er.get('err_msg', 'unknown')}")
        break

# --- Step 4: Summary ---
print(f"\n[4/4] Done! {len(done)}/{len(FILES)} succeeded.")
print(f"Results: {OUTPUT_DIR}/")
for d in sorted(os.listdir(OUTPUT_DIR)):
    full = os.path.join(OUTPUT_DIR, d)
    if os.path.isdir(full):
        contents = os.listdir(full)
        has_md = any(c.endswith('.md') or c == 'full.md' for c in contents)
        has_img = any(c == 'images' or c.startswith('images') for c in contents)
        print(f"  {d}/  (md={'✓' if has_md else '✗'}, images={'✓' if has_img else '✗'})")
