# Techcyte Object & Image Uploader (`upload_objects`)

A command-line Python utility to upload Whole Slide Images (WSI) and their associated annotations (GeoJSON) directly to the Techcyte digital pathology platform.

---

## Table of Contents

- [Overview & Techcyte Concepts](#overview--techcyte-concepts)
  - [The Techcyte Data Hierarchy](#the-techcyte-data-hierarchy)
- [Requirements](#requirements)
- [Installation](#installation)
- [Authentication & Environment Setup](#authentication--environment-setup)
- [Usage](#usage)
  - [Command-Line Arguments](#command-line-arguments)
  - [Example Command](#example-command)
- [Annotations & QuPath Compatibility](#annotations--qupath-compatibility)
  - [Supported Format](#supported-format)
  - [How Properties Are Mapped](#how-properties-are-mapped)
  - [Known Limits](#known-limits)
- [How It Works (Upload Lifecycle)](#how-it-works-upload-lifecycle)
- [Troubleshooting](#troubleshooting)

---

## Overview & Techcyte Concepts

If you are new to the Techcyte platform, it helps to understand how slides, images, and annotations are organized in Techcyte's data model:

### The Techcyte Data Hierarchy

```
Sample (Slide Container)
 └── Region (Image Layer / Whole Slide Image)
      └── Objects (Annotations / Polygons / Detections)
```

1. **Sample**:
   - Identified by a **barcode** (e.g., printed on the slide label or assigned by an LIS) and a **label** (typically the image filename).
2. **Region**:
   - Represents a specific scanned image layer associated with a Sample. A sample can have one or more regions (e.g., standard scan, z-stack, or re-scan).
   - The script creates a region under your sample and streams the raw image file directly into it.
   - Supports optional automatic backend conversion to **DICOM** format (`--convert`).
3. **Objects (Annotations)**:
   - In Techcyte's GraphQL API, geometric annotations are referred to as **Objects**.
   - These include polygonal boundaries, bounding boxes, labels/classifications, and viewer display colors.
   - Objects are linked directly to a **Region** and stamped with the authenticated user's ID as the author.

---

## Requirements

- **Python**: Version **3.13** or higher
- **Techcyte API Credentials**: A valid `client_id` and `client_secret` issued by Techcyte with permissions to create samples, regions, and objects.
- **Dependencies**:
  - `requests` (HTTP client)
  - `pydantic` (v2, data validation)
  - `tqdm` (progress bar for uploads)
  - `urllib3` (retry adapters)

---

## Installation

You can set up the environment using either [`uv`](https://github.com/astral-sh/uv).

```bash
# Clone the repository
git clone git@github.com:Techcyte/avio-uploader.git
cd avio-uploader

# Create virtual environment and sync dependencies
uv sync
```

## Authentication & Environment Setup

The script authenticates against the Techcyte platform via **OAuth 2.0 Client Credentials**. You must set your API credentials as environment variables before running the script:

```bash
export TECHCYTE_API_CLIENT_ID="your-client-id-here"
export TECHCYTE_API_CLIENT_SECRET="your-client-secret-here"
```

---

## Usage

Run `upload_objects.py` by providing the path to your whole slide image, the corresponding annotation GeoJSON file, and the slide barcode.

### Command-Line Arguments

| Argument | Type | Required? | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `--image` | `str` | **Yes** | — | Filepath to the whole slide image file (`.svs`, `.tif`, `.tiff`, etc.). |
| `--geojson` | `str` | **Yes** | — | Filepath to the GeoJSON annotation file (QuPath polygon export). |
| `--barcode` | `str` | **Yes** | — | Unique slide or specimen barcode identifier. |
| `--convert` / `--no-convert` | `bool` | No | `False` | When enabled, requests Techcyte to convert the image to DICOM upon upload. |
| `--host` | `str` | No | `https://api.app.techcyte.com` | Base URL of the Techcyte API endpoint. |

### Example Command

```bash
uv run upload_objects.py \
    --image /path/to/slide_001.svs \
    --geojson /path/to/slide_001_annotations.geojson \
    --barcode "SLIDE-2026-0910-A" \
    --convert
```

---

## Annotations & QuPath Compatibility

### Supported Format

The script is configured to parse **GeoJSON `FeatureCollection`** files containing polygon annotations, specifically those exported from [QuPath](https://qupath.github.io/):

- **Geometry**: Polygons with an outer ring coordinate list (`feature["geometry"]["coordinates"][0]`). Coordinates are in pixel units relative to the full-resolution slide image.
- **Bounding Box**: Automatically derived from the polygon vertices and rounded to integer pixel coordinates.

### How Properties Are Mapped

| QuPath GeoJSON Property | Techcyte Object Field | Notes |
| :--- | :--- | :--- |
| `classification.name` | `properties.name` | If missing, defaults to `Annotation 1`, `Annotation 2`, etc. |
| `classification.colorRGB` | `properties.color` | Converts signed 32-bit ARGB integers to standard hex format (`#RRGGBB`). |
| Current User ID (`me`) | `authorId`, `updatedBy` | Fetched dynamically from Techcyte GraphQL API using your access token. |
| Polygon Vertices | `geojson.geometry` | Rounded to whole integer pixel coordinates matching the Techcyte viewer. |

### Known Limits

> [!IMPORTANT]
> Techcyte's GraphQL `create_objects` mutation currently accepts up to **512 objects per batch**. If your GeoJSON file contains more than 512 annotations, you will need to batch the objects across multiple mutation calls.

---

## How It Works (Upload Lifecycle)

When you execute `upload_objects.py`, the following sequence takes place:

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
---

## Troubleshooting

### `401 Unauthorized`
- Ensure that both `TECHCYTE_API_CLIENT_ID` and `TECHCYTE_API_CLIENT_SECRET` are correctly exported in your current shell session.
- Verify that your client credentials are valid for the target host (e.g., production vs. staging).

### `KeyError: 'access_token'` or Token Failure
- Check if your credentials have expired or if your IP address is restricted by your organization's policy.
- Ensure the `--host` matches the environment your credentials were created for.

### `asyncio.TaskGroup` AttributeError
- Ensure you are running Python **3.11** or newer (`python --version`).

### `GraphQL mutation error: create_objects`
- Verify that the annotation file is a valid GeoJSON FeatureCollection with polygon geometries.
- Ensure the number of annotations in the file does not exceed the single-request limit of 512 objects.
