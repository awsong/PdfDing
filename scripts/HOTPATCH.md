# PdfDing hot-patch helper

`scripts/hotpatch_apply.sh` applies a set of modified source files into a
running PdfDing container without rebuilding the image. It is designed for
the runtime layout produced by the upstream `Dockerfile` (WhiteNoise +
`CompressedManifestStaticFilesStorage` under `/home/nonroot/pdfding`).

## Usage

```bash
scripts/hotpatch_apply.sh <container> <files_dir> [version_tag]
```

- `<container>` - docker container name or id running pdfding
- `<files_dir>` - a directory that mirrors the app tree, rooted at
  `files_dir/pdfding/...`
- `[version_tag]` - optional; if set and `files_dir` does not already
  contain `pdfding/core/settings/version.py`, the script writes
  `VERSION = '<version_tag>'` into that file.

### `files_dir` layout

Whatever files you want to change, put them under `<files_dir>/pdfding/`
mirroring the real app tree. Examples:

```
my-patch/
  pdfding/
    static/js/pdfding/viewer_logged_in.js   # -> patches source + hashed copy
    static/css/pdf_viewer.css               # -> patches source + hashed copy
    pdf/templates/viewer.html               # -> copied verbatim (no hash)
    core/settings/version.py                # -> optional override
```

## What the script does

1. Copies every regular file under `files_dir/pdfding/` into the matching
   path inside the container's `/home/nonroot/pdfding/`.
2. For each file under `pdfding/static/{js,css}/...`, locates the matching
   hashed sibling under `staticfiles/...`, overwrites it in place (keeping
   the original hash so `staticfiles.json` stays valid), and removes the
   stale `.gz` / `.br` pre-compressed files so WhiteNoise re-serves fresh
   bytes.
3. If `version_tag` is provided and no explicit `version.py` is in the
   patch, writes `core/settings/version.py` accordingly.
4. `docker restart`s the container so gunicorn reloads templates.

## Example: applying the autosave patch

```bash
# Assuming you have the extracted pdfding-autosave-hotpatch/ directory:
scripts/hotpatch_apply.sh pdfding pdfding-autosave-hotpatch/files 1.7.2-autosave
```

Or, working from the repo directly (files already live in `pdfding/`):

```bash
# Stage only the files you actually changed, then run the script:
mkdir -p /tmp/mypatch/pdfding/static/js/pdfding \
         /tmp/mypatch/pdfding/static/css \
         /tmp/mypatch/pdfding/pdf/templates
cp pdfding/static/js/pdfding/viewer_logged_in.js /tmp/mypatch/pdfding/static/js/pdfding/
cp pdfding/static/css/pdf_viewer.css             /tmp/mypatch/pdfding/static/css/
cp pdfding/pdf/templates/viewer.html             /tmp/mypatch/pdfding/pdf/templates/

scripts/hotpatch_apply.sh pdfding /tmp/mypatch 1.7.2-autosave
```

## After applying

- In the browser: hard-refresh (Cmd+Shift+R) to bypass the browser cache.
- Verify the version: `docker exec <container> cat /home/nonroot/pdfding/core/settings/version.py`

## Rollback

The script does not back up existing files. Before applying, snapshot the
container:

```bash
docker commit <container> pdfding:pre-patch-backup
```

## Limitations

- Only static files with an existing hashed sibling are handled; brand-new
  static files would require a real `collectstatic` run.
- Python source changes under `pdfding/` are copied but gunicorn may need
  a container restart (the script already restarts) and/or migrations to
  be run separately for model changes.
