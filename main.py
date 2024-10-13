import os
import uuid
import logging
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, HttpUrl
from pytube import YouTube
import ffmpeg
import re
from urllib.parse import urlparse, parse_qs

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI(title="Improved YouTube Video Downloader with Multiple Resolution Support")

# Constants
DOWNLOAD_DIR = "downloaded_videos"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

RESOLUTIONS = ["2160p", "1080p", "720p", "480p"]

# Models
class DownloadRequest(BaseModel):
    url: HttpUrl
    resolution: str

class DownloadStatus(BaseModel):
    task_id: str
    status: str
    filename: str = None
    error: str = None

# In-memory storage for download tasks
download_tasks = {}

# Helper functions
def safe_filename(filename):
    return re.sub(r'[^\w\-_\. ]', '_', filename)

def get_video_id(url):
    # Parse the URL to extract video ID
    parsed_url = urlparse(url)
    
    if parsed_url.hostname in ["www.youtube.com", "youtube.com"]:
        query_params = parse_qs(parsed_url.query)
        if "v" in query_params:
            return query_params["v"][0]  # Extract video ID from 'v' parameter
    elif parsed_url.hostname == "youtu.be":
        return parsed_url.path[1:]  # Extract video ID from short URL
    return None

async def download_and_process_video(task_id: str, url: str, target_resolution: str):
    task = download_tasks[task_id]
    task.status = "Downloading"
    
    try:
        logger.info(f"Starting download for task {task_id}: {url}")
        video_id = get_video_id(url)
        if not video_id:
            raise ValueError("Invalid YouTube URL")
        
        yt = YouTube(f"https://www.youtube.com/watch?v={video_id}")
        safe_title = safe_filename(yt.title)
        
        # Get the highest resolution stream
        stream = yt.streams.filter(progressive=False, file_extension="mp4").order_by("resolution").desc().first()
        
        if not stream:
            raise Exception("No suitable video stream found")
        
        logger.info(f"Downloading video: {stream.resolution}")
        video_file = os.path.join(DOWNLOAD_DIR, f"{task_id}_{safe_title}_video.mp4")
        stream.download(output_path=DOWNLOAD_DIR, filename=video_file)
        
        # Download audio separately
        audio_stream = yt.streams.filter(only_audio=True).first()
        if not audio_stream:
            raise Exception("No audio stream found")
        
        logger.info("Downloading audio")
        audio_file = os.path.join(DOWNLOAD_DIR, f"{task_id}_{safe_title}_audio.mp4")
        audio_stream.download(output_path=DOWNLOAD_DIR, filename=audio_file)
        
        logger.info(f"Download completed for task {task_id}. Starting processing.")
        task.status = "Processing"
        
        # Process video
        output_file = os.path.join(DOWNLOAD_DIR, f"{task_id}_{safe_title}_{target_resolution}.mp4")
        
        if target_resolution == "2160p":
            target_size = "3840x2160"
        elif target_resolution == "1080p":
            target_size = "1920x1080"
        elif target_resolution == "720p":
            target_size = "1280x720"
        elif target_resolution == "480p":
            target_size = "854x480"
        else:
            raise ValueError(f"Invalid resolution: {target_resolution}")
        
        logger.info(f"Processing video to {target_resolution}")
        (
            ffmpeg
            .input(video_file)
            .output(output_file, vf=f"scale={target_size}:force_original_aspect_ratio=decrease,pad={target_size}:-1:-1:color=black", 
                    acodec="copy", vcodec="libx264", preset="medium")
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )
        
        # Clean up temporary files
        os.remove(video_file)
        os.remove(audio_file)
        
        logger.info(f"Processing completed for task {task_id}.")
        task.status = "Completed"
        task.filename = os.path.basename(output_file)
    except Exception as e:
        logger.error(f"Error in task {task_id}: {str(e)}", exc_info=True)
        task.status = "Failed"
        task.error = str(e)

# API endpoints
@app.post("/download")
async def request_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    try:
        if request.resolution not in RESOLUTIONS:
            raise HTTPException(status_code=400, detail=f"Invalid resolution. Supported resolutions are: {', '.join(RESOLUTIONS)}")
        
        task_id = str(uuid.uuid4())
        download_tasks[task_id] = DownloadStatus(task_id=task_id, status="Queued")
        
        logger.info(f"New download request: {request.url}, Resolution: {request.resolution}")
        background_tasks.add_task(download_and_process_video, task_id, str(request.url), request.resolution)
        
        return JSONResponse(content={"task_id": task_id, "message": "Download request accepted"})
    except Exception as e:
        logger.error(f"Error in request_download: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/status/{task_id}")
async def get_status(task_id: str):
    task = download_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@app.get("/download/{task_id}")
async def download_file(task_id: str):
    task = download_tasks.get(task_id)
    if not task or task.status != "Completed":
        raise HTTPException(status_code=404, detail="Download not ready or doesn't exist")
    
    file_path = os.path.join(DOWNLOAD_DIR, task.filename)
    return FileResponse(file_path, media_type="video/mp4", filename=task.filename)

@app.on_event("shutdown")
async def shutdown_event():
    for file in os.listdir(DOWNLOAD_DIR):
        os.remove(os.path.join(DOWNLOAD_DIR, file))
    os.rmdir(DOWNLOAD_DIR)
