import io
import json
import os
import shutil
import tempfile
import traceback
from typing import Optional, Tuple
from urllib.parse import unquote, urlparse

import boto3
import internetarchive
from PIL import Image, ImageOps, UnidentifiedImageError

REGION = os.getenv("AWS_REGION", "us-east-2")
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "80"))
DEFAULT_MAX_WIDTH = int(os.getenv("DEFAULT_MAX_WIDTH", "1500"))
DEFAULT_MAX_HEIGHT = int(os.getenv("DEFAULT_MAX_HEIGHT", "1500"))
IA_RETRIES = int(os.getenv("IA_RETRIES", "5"))

ALLOWED_FORMATS = {"JPEG", "PNG"}

s3 = boto3.client("s3", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)


def _parse_int(value, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _extract_ia_identifier_and_file(access_uri: str) -> Tuple[str, str]:
    """Extract IA identifier and file path from common IA URL variants."""
    parsed = urlparse(access_uri.strip())
    path = parsed.path or ""

    download_prefix = "/download/"
    if download_prefix in path:
        after = path.split(download_prefix, 1)[1].lstrip("/")
        parts = after.split("/", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return unquote(parts[0]), unquote(parts[1])

    items_marker = "/items/"
    if items_marker in path:
        after = path.split(items_marker, 1)[1].lstrip("/")
        parts = after.split("/", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return unquote(parts[0]), unquote(parts[1])

    raise ValueError(f"Unable to parse Internet Archive identifier/file from URL: {access_uri}")


def _download_from_internet_archive(access_uri: str, work_dir: str) -> bytes:
    identifier, ia_file = _extract_ia_identifier_and_file(access_uri)

    # Use no_directory=True so each attempt writes directly into the temp folder.
    internetarchive.download(
        identifier,
        files=[ia_file],
        destdir=work_dir,
        ignore_existing=True,
        retries=IA_RETRIES,
        no_directory=True,
        checksum=False,
        verbose=False,
    )

    expected = os.path.join(work_dir, ia_file)
    if os.path.exists(expected):
        with open(expected, "rb") as fh:
            return fh.read()

    expected_basename = os.path.join(work_dir, os.path.basename(ia_file))
    if os.path.exists(expected_basename):
        with open(expected_basename, "rb") as fh:
            return fh.read()

    # Fallback: locate the downloaded file anywhere under the temp folder.
    for root, _, file_names in os.walk(work_dir):
        for file_name in file_names:
            file_path = os.path.join(root, file_name)
            if os.path.isfile(file_path):
                with open(file_path, "rb") as fh:
                    return fh.read()

    raise FileNotFoundError(
        f"internetarchive.download completed but no file was found for item={identifier}, file={ia_file}"
    )


def _resize_to_jpeg(image_bytes: bytes, max_width: int, max_height: int) -> bytes:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if (image.format or "").upper() not in ALLOWED_FORMATS:
                raise ValueError(f"Unsupported image type: {image.format or 'unknown'}")

            image = ImageOps.exif_transpose(image)

            # Mimics sharp fit: inside + withoutEnlargement.
            width, height = image.size
            scale = min(max_width / width, max_height / height, 1.0)
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))

            if new_size != image.size:
                image = image.resize(new_size, Image.Resampling.LANCZOS)

            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            elif image.mode == "L":
                image = image.convert("RGB")

            output = io.BytesIO()
            image.save(output, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return output.getvalue()

    except UnidentifiedImageError as exc:
        raise ValueError("Unsupported image type: unknown") from exc


def _send_status(
    updates_queue_url: str,
    queue_id,
    file_id,
    subject_id,
    status: str,
    task_type: str,
    error: Optional[str] = None,
) -> None:
    payload = {
        "queueId": str(queue_id or ""),
        "fileId": int(file_id or 0),
        "subjectId": str(subject_id or ""),
        "status": status,
        "function": "BiospexImageProcess" if task_type == "export" else "BiospexOcrProcessor",
    }
    if error:
        payload["error"] = error

    sqs.send_message(QueueUrl=updates_queue_url, MessageBody=json.dumps(payload))


def _process_record(message: dict) -> dict:
    task_type = message.get("taskType")
    access_uri = message.get("accessURI")
    subject_id = message.get("subjectId")
    s3_bucket = message.get("s3Bucket")
    s3_path = message.get("s3Path")
    updates_queue_url = message.get("updatesQueueUrl")
    queue_id = message.get("queueId")
    file_id = message.get("fileId")
    max_width = _parse_int(message.get("maxWidth"), DEFAULT_MAX_WIDTH)
    max_height = _parse_int(message.get("maxHeight"), DEFAULT_MAX_HEIGHT)

    if not access_uri:
        raise ValueError("Missing required field: accessURI")
    if not s3_bucket or not s3_path:
        raise ValueError("Missing required field(s): s3Bucket and/or s3Path")
    if not updates_queue_url:
        raise ValueError("Missing required field: updatesQueueUrl")

    temp_dir = tempfile.mkdtemp(prefix="iafetch-")
    try:
        source_bytes = _download_from_internet_archive(access_uri, temp_dir)
        processed_image = _resize_to_jpeg(source_bytes, max_width, max_height)

        s3.put_object(
            Bucket=s3_bucket,
            Key=s3_path,
            Body=processed_image,
            ContentType="image/jpeg",
            Metadata={
                "queue-id": str(queue_id or ""),
                "file-id": str(file_id or ""),
                "subject-id": str(subject_id or ""),
                "updates-url": str(updates_queue_url),
            },
        )

        # Keep drop-in behavior aligned with BiospexImageFetcher.
        if task_type == "export":
            _send_status(updates_queue_url, queue_id, file_id, subject_id, "success", task_type)

        return {"success": True, "subjectId": subject_id}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def lambda_handler(event, context):
    responses = []

    for record in event.get("Records", []):
        subject_id = None
        try:
            message = json.loads(record.get("body", "{}"))
            subject_id = message.get("subjectId")
            result = _process_record(message)
            responses.append(result)
        except Exception as exc:
            message = {}
            try:
                message = json.loads(record.get("body", "{}"))
            except Exception:
                pass

            task_type = message.get("taskType", "ocr")
            updates_queue_url = message.get("updatesQueueUrl")
            queue_id = message.get("queueId")
            file_id = message.get("fileId")
            subject_id = subject_id or message.get("subjectId")

            error_text = f"Fetcher: {str(exc)}"
            print(f"Fetcher failed for subject {subject_id}: {error_text}")
            print(traceback.format_exc())

            if updates_queue_url:
                _send_status(
                    updates_queue_url,
                    queue_id,
                    file_id,
                    subject_id,
                    "failed",
                    task_type,
                    error=error_text,
                )

            responses.append({"success": False, "subjectId": subject_id, "error": str(exc)})

    return {"statusCode": 200, "body": json.dumps({"processed": responses})}

