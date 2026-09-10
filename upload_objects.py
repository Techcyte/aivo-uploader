"""
This script will take an image file and a annotation and upload them into the techcyte
system. The script requires the user to configure two environment variables.

Requirements:
    tqdm
    pydantic
    requests
    urllib3

Environment:
    TECHCYTE_API_CLIENT_ID
    TECHCYTE_API_CLIENT_SECRET

Arguments:
    --image: the path to an image file (svs, tiff, etc)
    --geojson: the path to the annotation data
    --barcode: barcode of the scan
    --convert: (bool, optional) will trigger conversion to dicom upon upload
    --host: (optional) defaults to https://api.app.techcyte.com, override if you want to upload to ci or staging
"""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from pydantic import BaseModel, ConfigDict
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util import Retry


class Object(BaseModel):
    """Objects come from the backend looking like this, examles with geojson are below:

    {'confidence': 0, 'height': 158, 'id': '181859', 'object_type_id': 'toxocara', 'region_id': '136', 'width': 196, 'x': 26147, 'y': 4303}


    this class just gives you something to work with to crate your objects that isn't
    raw json pydantic will do the typchecking for you to make sure everything is in place.
    I've called it `Objects` because that is the name we use in the gql endpoint,
    but these are annotations
    """

    model_config = ConfigDict(strict=True)

    object_type_id: str
    x: int
    y: int
    width: int
    height: int
    confidence: Optional[float]
    region_id: int | str
    geojson: Optional[str]


def example_object_json():
    """
    this function just holds some example objects that I created to test this script
    they may be helpful in showing an example of what we store
    """
    object_json = [
        {
            "confidence": None,
            "geojson": '{"bbox": [57147, 15310, 61095, 19258], "type": "Feature", "authorId": "2952491", "geometry": {"type": "Polygon", "coordinates": [[[60375, 18809], [60277, 18885], [60174, 18954], [60067, 19017], [59956, 19073], [59842, 19122], [59725, 19164], [59606, 19198], [59485, 19225], [59363, 19244], [59239, 19255], [59115, 19258], [58991, 19254], [58868, 19242], [58745, 19222], [58624, 19195], [58505, 19160], [58389, 19118], [58275, 19068], [58165, 19011], [58058, 18948], [57956, 18878], [57858, 18802], [57765, 18719], [57678, 18631], [57596, 18538], [57520, 18440], [57451, 18337], [57388, 18230], [57332, 18119], [57283, 18005], [57241, 17889], [57207, 17769], [57180, 17648], [57161, 17526], [57150, 17402], [57147, 17278], [57151, 17154], [57163, 17031], [57183, 16908], [57210, 16787], [57245, 16668], [57287, 16552], [57337, 16438], [57394, 16328], [57457, 16221], [57527, 16119], [57603, 16021], [57686, 15928], [57774, 15841], [57867, 15759], [57965, 15683], [58068, 15614], [58175, 15551], [58286, 15495], [58399, 15446], [58516, 15405], [58636, 15370], [58757, 15344], [58879, 15325], [59003, 15313], [59127, 15310], [59251, 15314], [59374, 15326], [59497, 15346], [59617, 15373], [59737, 15408], [59853, 15451], [59967, 15500], [60077, 15557], [60184, 15620], [60286, 15690], [60384, 15767], [60477, 15849], [60564, 15937], [60646, 16030], [60722, 16128], [60791, 16231], [60854, 16338], [60910, 16449], [60959, 16563], [61000, 16680], [61035, 16799], [61061, 16920], [61080, 17042], [61092, 17166], [61095, 17290], [61091, 17414], [61079, 17537], [61059, 17660], [61032, 17781], [60997, 17900], [60954, 18016], [60905, 18130], [60848, 18240], [60785, 18347], [60715, 18449], [60638, 18547], [60556, 18640], [60468, 18727], [60375, 18809]]]}, "updatedBy": "2952491", "properties": {"kind": "ellipse", "name": "Test Annotation", "color": "#000000", "marked": false, "authorId": "2952491", "controlPoints": [{"x": 57866.8676670792, "y": 15759.14204826732}, {"x": 60374.83641707919, "y": 18809.06392326732}, {"x": 57595.8911045792, "y": 18538.087360767313}]}}',
            "height": 3948,
            "object_type_id": "annotation",
            "region_id": "18028553",
            "width": 3948,
            "x": 57147,
            "y": 15310,
        },
        {
            "confidence": None,
            "geojson": '{"bbox": [63852, 6620, 69603, 12372], "type": "Feature", "authorId": "2952491", "geometry": {"type": "Polygon", "coordinates": [[[69274, 10833], [69186, 10990], [69087, 11141], [68979, 11286], [68862, 11424], [68737, 11554], [68604, 11676], [68463, 11790], [68315, 11894], [68162, 11989], [68002, 12074], [67838, 12149], [67669, 12214], [67497, 12268], [67321, 12310], [67143, 12342], [66964, 12363], [66783, 12372], [66602, 12370], [66422, 12356], [66243, 12331], [66066, 12295], [65892, 12248], [65721, 12190], [65553, 12122], [65391, 12043], [65234, 11954], [65082, 11855], [64937, 11747], [64800, 11630], [64669, 11505], [64547, 11372], [64434, 11231], [64329, 11084], [64234, 10930], [64149, 10771], [64074, 10606], [64010, 10437], [63956, 10265], [63913, 10089], [63882, 9911], [63861, 9732], [63852, 9552], [63854, 9371], [63868, 9191], [63892, 9012], [63928, 8835], [63976, 8660], [64033, 8489], [64102, 8322], [64181, 8159], [64270, 8002], [64369, 7851], [64476, 7706], [64593, 7568], [64719, 7438], [64852, 7316], [64992, 7202], [65140, 7098], [65294, 7003], [65453, 6918], [65617, 6843], [65786, 6778], [65959, 6724], [66134, 6682], [66312, 6650], [66492, 6629], [66672, 6620], [66853, 6622], [67033, 6636], [67212, 6661], [67389, 6697], [67564, 6744], [67735, 6802], [67902, 6870], [68064, 6949], [68222, 7038], [68373, 7137], [68518, 7245], [68656, 7362], [68786, 7487], [68908, 7620], [69022, 7761], [69126, 7908], [69221, 8062], [69306, 8221], [69381, 8386], [69446, 8555], [69499, 8727], [69542, 8903], [69574, 9081], [69594, 9260], [69603, 9441], [69601, 9621], [69588, 9801], [69563, 9980], [69527, 10157], [69480, 10332], [69422, 10503], [69353, 10670], [69274, 10833]]]}, "updatedBy": "2952491", "properties": {"kind": "ellipse", "name": "Test 2", "color": "#000000", "marked": false, "authorId": "2952491", "controlPoints": [{"x": 64180.89079455934, "y": 8159.218749999996}, {"x": 69274.48454455934, "y": 10832.812499999993}, {"x": 65390.89079455934, "y": 12042.812499999996}]}}',
            "height": 5752,
            "object_type_id": "annotation",
            "region_id": "18028553",
            "width": 5751,
            "x": 63852,
            "y": 6620,
        },
    ]
    objects = [Object(**d) for d in object_json]
    return objects


def get_token(host: str) -> str:
    client_id = os.environ.get("TECHCYTE_API_CLIENT_ID")
    client_secret = os.environ.get("TECHCYTE_API_CLIENT_SECRET")
    response = requests.request(
        "POST",
        urljoin(host, "api/v3/token"),
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    response.raise_for_status()
    response_json = response.json()

    return response_json["access_token"]


def get_current_user_id(host: str, token: str) -> str:
    """the id of the user the token belongs to, used as the author of the annotations"""
    query = """query {
        me {
            user {
                decodedId
            }
        }
    }
    """
    response = requests.request(
        "POST",
        urljoin(host, "api/graphql"),
        headers={
            "Authorization": f"Bearer {token}",
        },
        json={"query": query},
    )
    response.raise_for_status()
    return response.json()["data"]["me"]["user"]["decodedId"]


def create_sample(host: str, token: str, sample_data: dict) -> dict:
    """we should set the label and barcode of the sample to be the name of the file"""
    response = requests.request(
        "POST",
        urljoin(host, "api/v3/samples"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(sample_data),
    )
    response.raise_for_status()
    return response.json()


def create_region(
    host: str, token: str, sample_id: str | int, region_data: dict
) -> dict:
    """we should set the original filename on the region"""
    response = requests.request(
        "POST",
        urljoin(host, f"api/v3/samples/{sample_id}/regions"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(region_data),
    )
    response.raise_for_status()
    return response.json()


def convert_raw_objects_to_techcyte(
    raw_objects, region_id: int | str, author_id: str
) -> list[Object]:
    """Convert a QuPath geojson FeatureCollection of polygons into techcyte objects.

    The source features have no bounding box, so we derive it from the polygon's
    points, which are also rounded to whole pixels to match what the viewer stores.
    """
    objects = []
    unnamed_count = 0
    for feature in raw_objects["features"]:
        # the outer ring is all we need, these polygons have no holes
        points = [
            {"x": round(x), "y": round(y)}
            for x, y in feature["geometry"]["coordinates"][0]
        ]
        min_x = min(point["x"] for point in points)
        min_y = min(point["y"] for point in points)
        max_x = max(point["x"] for point in points)
        max_y = max(point["y"] for point in points)

        properties = feature["properties"]
        classification = properties.get("classification", {})
        # QuPath stores colors as a signed 32 bit ARGB int, we want #RRGGBB
        color = "#{:06X}".format(classification.get("colorRGB", 0) & 0xFFFFFF)

        name = classification.get("name")
        if not name:
            unnamed_count += 1
            name = f"Annotation {unnamed_count}"

        geojson = {
            "bbox": [min_x, min_y, max_x, max_y],
            "type": "Feature",
            "authorId": author_id,
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[point["x"], point["y"]] for point in points]],
            },
            "updatedBy": author_id,
            "properties": {
                "kind": "polygon",
                "name": name,
                "color": color,
                "marked": False,
            },
        }

        objects.append(
            Object(
                object_type_id="annotation",
                x=min_x,
                y=min_y,
                width=max_x - min_x,
                height=max_y - min_y,
                confidence=None,
                region_id=region_id,
                geojson=json.dumps(geojson),
            )
        )

    return objects


def load_annotations_from_json(
    filename: str, region_id: int | str, author_id: str
) -> list[Object]:
    with open(filename, "r") as fp:
        raw_objects = json.load(fp)

    techcyte_objects = convert_raw_objects_to_techcyte(
        raw_objects, region_id, author_id
    )
    return techcyte_objects


def create_objects(host: str, token: str, objects: list[Object]):
    """gql endpoint to create the objects

    WARNING: this endpoint only lets you create 512 objects at a time, if you need more you'll need
    to chunk your request.
    """
    mutation = """mutation (
        $objects: [CreateObjectInput]
    ) {
        create_objects(
            objects: $objects
        ) {
            objects {
                id
            }
        }
    }
    """
    variables = {"objects": [obj.model_dump() for obj in objects]}
    response = requests.request(
        "POST",
        urljoin(host, "api/graphql"),
        headers={
            "Authorization": f"Bearer {token}",
        },
        json={"query": mutation, "variables": variables},
    )
    response.raise_for_status()
    return response.json()


def start_region_upload(host: str, token: str, region_id: str | int) -> dict:
    response = requests.request(
        "POST",
        urljoin(host, f"api/v3/regions/{region_id}/uploads"),
        headers={
            "Authorization": f"Bearer {token}",
        },
    )
    response.raise_for_status()
    return response.json()


def finish_region_upload(
    host: str, token: str, upload_id: str | int, etags
) -> requests.Response:
    response = requests.request(
        "POST",
        urljoin(host, f"api/v3/uploads/{upload_id}/done"),
        headers={
            "Authorization": f"Bearer {token}",
        },
        data=json.dumps(etags),
    )
    response.raise_for_status()
    return response


def mark_region_uploaded(host: str, token: str, region_id: str | int):
    response = requests.request(
        "POST",
        urljoin(host, f"api/v3/regions/{region_id}/done"),
        headers={
            "Authorization": f"Bearer {token}",
        },
    )
    response.raise_for_status()
    return response


def mark_sample_uploaded(host: str, token: str, sample_id: str | int):
    response = requests.request(
        "PATCH",
        urljoin(host, f"api/v3/samples/{sample_id}"),
        headers={
            "Authorization": f"Bearer {token}",
        },
        data=json.dumps({"uploaded": True}),
    )
    response.raise_for_status()
    return response


def get_part_upload_url(
    host: str, token: str, upload_id: int | str, part_index: int
) -> str:
    response = requests.request(
        "GET",
        urljoin(host, f"api/v3/uploads/{upload_id}/parts/{part_index+1}"),
        headers={
            "Authorization": f"Bearer {token}",
        },
    )
    response.raise_for_status()
    return response.json()["location"]


async def upload_part(
    host: str,
    session: requests.Session,
    token,
    upload_id: str,
    part_index: int,
    blob: bytes,
    pbar: tqdm,
) -> str:
    url = get_part_upload_url(host, token, upload_id, part_index)
    response = session.request(
        "PUT",
        url,
        data=blob,
    )
    response.raise_for_status()
    pbar.update(1)
    return response.headers["ETag"]


async def upload_file(host: str, token: str, filepath: str, region_id: str) -> None:
    upload = start_region_upload(host, token, region_id)

    file_bytes = os.path.getsize(filepath)
    part_size = 10_000_000  # 5MB is the min size
    total_parts = math.ceil(file_bytes / part_size)
    print(f"Uploading file in {total_parts} 10MB parts")
    session = requests.Session()
    retry = Retry(total=5, backoff_factor=0.1)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    tasks = []
    with tqdm(total=total_parts, desc="Uploading file") as pbar:
        with open(filepath, "rb") as fp:
            async with asyncio.TaskGroup() as tg:
                for part in range(total_parts):
                    task = tg.create_task(
                        upload_part(
                            host,
                            session,
                            token,
                            upload["id"],
                            part,
                            fp.read(part_size),
                            pbar,
                        )
                    )
                    tasks.append(task)
    finish_region_upload(host, token, upload["id"], [task.result() for task in tasks])


def main(args):
    access_token = get_token(args.host)

    image_filepath = Path(args.image)
    sample_data = {
        "label": image_filepath.name,
        "barcode": args.barcode,
    }

    sample = create_sample(args.host, access_token, sample_data)
    print("created sample", sample["id"])

    region_data = {
        "original_filename": image_filepath.name,
    }
    if args.convert:
        region_data["mimetype"] = "image/tiff+convert"

    region = create_region(args.host, access_token, sample["id"], region_data)
    print("created region", region["id"])

    # once the file is uploaded the region and the sample need to be marked us "uploaded"
    asyncio.run(
        upload_file(args.host, access_token, image_filepath.as_posix(), region["id"])
    )
    mark_region_uploaded(args.host, access_token, region["id"])
    mark_sample_uploaded(args.host, access_token, sample["id"])

    # load the objects from the json file and convert them to the techcyte format
    author_id = get_current_user_id(args.host, access_token)
    objects = load_annotations_from_json(args.geojson, region["id"], author_id)

    # create the objects
    response_json = create_objects(args.host, access_token, objects)
    print(
        f"created {len(response_json['data']['create_objects']['objects'])} objects for sample {sample['id']}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", help="file path to image file", required=True)
    parser.add_argument("--geojson", help="file path to geojson file", required=True)
    parser.add_argument("--host", default="https://api.app.techcyte.com")
    parser.add_argument("--barcode", required=True)
    parser.add_argument(
        "--convert",
        type=bool,
        default=False,
        help="convert the file to dicom upon upload",
        action=argparse.BooleanOptionalAction,
    )
    args = parser.parse_args()
    main(args)
