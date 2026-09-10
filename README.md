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
- **Techcyte API Credentials**: A `client_id` and `client_secret` with permissions to create samples, regions, and objects.

## Installation

Clone the repository and sync dependencies with [`uv`](https://github.com/astral-sh/uv):

```bash
git clone git@github.com:Techcyte/avio-uploader.git
cd avio-uploader
uv sync
```

## Authentication & Environment Setup

Set your Techcyte API credentials as environment variables before running the script:

```bash
export TECHCYTE_API_CLIENT_ID="your-client-id-here"
export TECHCYTE_API_CLIENT_SECRET="your-client-secret-here"
```

By default, the script connects to production (`https://api.app.techcyte.com`). To target a staging or CI environment, pass `--host`.

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

## Troubleshooting

- **`401 Unauthorized`**: Verify `TECHCYTE_API_CLIENT_ID` and `TECHCYTE_API_CLIENT_SECRET` are set and valid for the target `--host`.
- **`KeyError: 'access_token'`**: Check credentials and host URL.
- **`asyncio.TaskGroup` error**: Ensure you are running Python 3.11+ (Python 3.13 recommended).
- **`GraphQL mutation error: create_objects`**: Ensure the GeoJSON contains valid polygons and does not exceed the 512-object limit.
