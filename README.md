# Techcyte Object & Image Uploader (`upload_objects`)

A command-line tool to upload Whole Slide Images (WSI) and associated annotations (GeoJSON) to the Techcyte digital pathology platform.

## Table of Contents

- [Overview & Techcyte Concepts](#overview-&-techcyte-concepts)
- [Requirements](#requirements)
- [Installation](#installation)
- [Authentication & Environment Setup](#authentication-&-environment-setup)
- [Usage](#usage)
- [Annotations & QuPath Compatibility](#annotations-&-qupath-compatibility)
- [Upload Lifecycle](#upload-lifecycle)
- [Downloading Scans for Tasks](#downloading-scans-for-tasks)
- [Troubleshooting](#troubleshooting)

## Overview & Techcyte Concepts

Techcyte organizes data in a three-level hierarchy:

```
Sample (Slide Container)
 └── Region (Image Layer / Whole Slide Image)
      └── Objects (Annotations / Polygons / Detections)
```

1. **Sample**: The container for a physical slide or specimen, identified by a **barcode** and a **label** (typically the image filename).
2. **Region**: A scanned image layer under a sample. The script creates a region, uploads the image file, and optionally requests DICOM conversion (`--convert`).
3. **Objects (Annotations)**: Geometric shapes (polygons, bounding boxes, labels) linked to a region and authored by the authenticated user.

## Requirements

- **Python**: 3.13+
- **[uv](https://github.com/astral-sh/uv)** package manager
- **Techcyte API Credentials**: A `client_id` and `client_secret` with permissions to create samples, regions, and objects (uploading), or to read cases, tasks, and scans and edit tasks (downloading).

## Installation

Clone the repository and sync dependencies with [`uv`](https://github.com/astral-sh/uv):

```bash
git clone git@github.com:Techcyte/avio-uploader.git
cd avio-uploader
uv sync
```

## Authentication & Environment Setup

Both scripts read your Techcyte API credentials from environment variables. Set them before running either one:

```bash
export TECHCYTE_API_CLIENT_ID="your-client-id-here"
export TECHCYTE_API_CLIENT_SECRET="your-client-secret-here"
```

By default, both scripts connect to production (`https://api.app.techcyte.com`). To target a staging or CI environment, pass `--host`.

## Usage

Run `upload_objects.py` with the image path, GeoJSON annotation file, and slide barcode:

```bash
uv run upload_objects.py \
    --image /path/to/slide_001.svs \
    --geojson /path/to/slide_001_annotations.geojson \
    --barcode "SLIDE-2026-0910-A" \
    --convert
```

### Command-Line Arguments

| Argument | Type | Required? | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `--image` | `str` | **Yes** | — | Path to the slide image file (`.svs`, `.tif`, `.tiff`, etc.). |
| `--geojson` | `str` | **Yes** | — | Path to the GeoJSON annotation file (QuPath polygon export). |
| `--barcode` | `str` | **Yes** | — | Slide or specimen barcode identifier. |
| `--convert` / `--no-convert` | `bool` | No | `False` | Trigger backend conversion to DICOM upon upload. |
| `--host` | `str` | No | `https://api.app.techcyte.com` | Techcyte API base URL. |

## Annotations & QuPath Compatibility

The script parses **GeoJSON `FeatureCollection`** files containing polygon annotations exported from [QuPath](https://qupath.github.io/):

- **Geometry**: Polygons using the outer coordinate ring (`geometry.coordinates[0]`). Coordinates are in pixel units relative to the full-resolution image and rounded to integers.
- **Bounding Box**: Derived automatically from polygon vertices (`x`, `y`, `width`, `height`).

### Property Mapping

| QuPath Property | Techcyte Object Field | Notes |
| :--- | :--- | :--- |
| `classification.name` | `properties.name` | Defaults to `Annotation 1`, `Annotation 2`, etc. if missing. |
| `classification.colorRGB` | `properties.color` | Converts signed 32-bit ARGB integers to `#RRGGBB` hex format. |
| Token owner | `authorId`, `updatedBy` | Resolved from the current user via GraphQL `me.user.decodedId`. |

> **Note:** Techcyte's GraphQL `create_objects` mutation accepts up to **512 objects per batch**. Files with more than 512 annotations must be chunked into multiple requests.

## Upload Lifecycle

When executed, `upload_objects.py` runs through the following sequence:

```
1. Authenticate (OAuth2 Token)
   │
2. Create Sample (Barcode & Filename)
   │
3. Create Region (MIME / DICOM conversion flags)
   │
4. Multipart Chunked Upload (10 MB chunks in parallel with retries)
   │
5. Finalize Region & Sample (Mark 'done' & 'uploaded')
   │
6. Fetch Current User ID (GraphQL me.user.decodedId)
   │
7. Convert GeoJSON & Post Objects (GraphQL create_objects)
```

## Downloading Scans for Tasks

`download_scans.py` finds every **not started** task of a given task type in a company, downloads the scans attached to each one, and moves the task through its lifecycle as it goes. Like the uploader, it reads credentials from `TECHCYTE_API_CLIENT_ID` and `TECHCYTE_API_CLIENT_SECRET` (see [Authentication & Environment Setup](#authentication-&-environment-setup)).

```bash
uv run download_scans.py \
    --company-id 1234 \
    --task-type-id 56 \
    --dest ./scans \
    --dry-run
```

Drop `--dry-run` to download. A dry run lists the matching tasks, their scans, and how much data they hold, without downloading anything or changing any task.

| Argument | Type | Required? | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `--task-type-id` | `str` | **Yes** | — | Task (request) type to look for: the ID from **Configuration → Request types** (`321083`), or its encoded GraphQL ID. See [Finding the task type ID](#finding-the-task-type-id). |
| `--dest` | `path` | **Yes** | — | Directory to download into. |
| `--company-id` | `str` | No | API client's company | Company to search. A different company requires the client to be allowed to impersonate it. |
| `--host` | `str` | No | `https://api.app.techcyte.com` | Techcyte API base URL. |
| `--overwrite` / `--no-overwrite` | `bool` | No | `False` | Re-download files already on disk. |
| `--dicom-annotations` / `--no-dicom-annotations` | `bool` | No | `False` | Include the scan's annotations in each DICOM zip. See [DICOM annotations](#dicom-annotations). |
| `--dry-run` / `--no-dry-run` | `bool` | No | `False` | List tasks, scans, and sizes only. |
| `--workers` | `int` | No | `4` | Number of scans to download at once. |

Files land in `<dest>/<case_number>/<task_id>/<scan_id>/`. DICOM scans are saved as a `.zip`.

### DICOM annotations

With `--dicom-annotations`, each DICOM scan's zip also contains `dicom/techcyte_annotations.dcm`, a DICOM file holding the scan's annotations. Things to know:

- It only applies to DICOM scans. Other scans download as usual.
- A scan with no annotations gets no annotations file.
- Scans that haven't been processed yet download as their original DICOM, without annotations.
- If the server can't export a scan's annotations, that scan fails and its task is reset to NOT_STARTED.
- Scans already on disk are skipped, so to add annotations to them, rerun with `--overwrite` as well.

### Finding the task type ID

Task types are called **request types** in the Techcyte app. To find the ID to pass as `--task-type-id`:

1. Open **Configuration** from the navigation menu.
2. Select the **Request types** tab.
3. Copy the number in the **ID** column for the request type you want (e.g. `321083`).

### Task Lifecycle

```
1. Find NOT_STARTED tasks of the task type (and size their scans)
   │
2. Download scans --workers at a time, each with its own progress bar
   (a task is marked IN_PROGRESS when its first scan starts)
   │
3. When a task's last scan finishes:
   All its scans succeeded → complete the task
   Any scan failed         → reset the task to NOT_STARTED for the next run
```

Finished files are remembered, so rerunning after a failure only downloads what's missing. Pressing Ctrl-C stops the downloads in progress and resets every task it had claimed to NOT_STARTED. Run one copy at a time: two concurrent runs can claim the same task.

## Troubleshooting

- **`403 Forbidden` from `download_scans.py`**: The API client needs permission to read cases, tasks, and scans and to edit tasks in the target company (and to impersonate it, if `--company-id` isn't its own).
- **`401 Unauthorized`**: Verify `TECHCYTE_API_CLIENT_ID` and `TECHCYTE_API_CLIENT_SECRET` are set and valid for the target `--host`.
- **`KeyError: 'access_token'`**: Check credentials and host URL.
- **`asyncio.TaskGroup` error**: Ensure you are running Python 3.11+ (Python 3.13 recommended).
- **`GraphQL mutation error: create_objects`**: Ensure the GeoJSON contains valid polygons and does not exceed the 512-object limit.
