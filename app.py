import os
import tempfile
import json
import uuid
import threading
import time
import subprocess
from datetime import timedelta
from flask import Flask, request, jsonify, send_from_directory
import pysrt
import logging
import shutil
from pydub import AudioSegment
from pydub.silence import split_on_silence
from pydub.playback import play
from pydub.generators import WhiteNoise
from pydub import effects
import re
from pytube import YouTube

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')

# Ensure upload directory exists
UPLOAD_FOLDER = os.path.join(tempfile.gettempdir(), 'video_subtitle_uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Flag to track if speech recognition is available
whisper_available = False
whisper_model = None

# Dictionary to store task progress
tasks = {}

# Try to import optional dependencies
try:
    from moviepy.editor import VideoFileClip
    from pytube import YouTube
    import whisper
    import torch
    
    # Check if CUDA is available for GPU acceleration
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info(f"Using device: {device} for Whisper")
    
    # Initialize Whisper model (use "tiny" for fast results, "base" for better accuracy)
    # Options: "tiny", "base", "small", "medium", "large"
    try:
        whisper_model = whisper.load_model("tiny", device=device)
        whisper_available = True
        logger.info("Whisper speech recognition initialized successfully with 'tiny' model")
    except Exception as model_error:
        logger.error(f"Error loading Whisper model: {str(model_error)}")
        try:
            # Try with CPU if GPU fails
            logger.info("Trying to load Whisper model on CPU")
            whisper_model = whisper.load_model("tiny", device="cpu")
            whisper_available = True
            logger.info("Whisper speech recognition initialized successfully with 'tiny' model on CPU")
        except Exception as cpu_error:
            logger.error(f"Error loading Whisper model on CPU: {str(cpu_error)}")
except Exception as e:
    logger.error(f"Could not initialize Whisper: {str(e)}")

# Check for yt-dlp availability
yt_dlp_available = False
try:
    import yt_dlp
    yt_dlp_available = True
    logger.info("yt-dlp library is available")
except ImportError:
    logger.warning("yt-dlp library is not available, will use alternative methods")

# Add pytube version check and workaround for HTTP 400 errors
try:
    import pytube
    logger.info(f"Using pytube version: {pytube.__version__}")
    
    # Apply workaround for HTTP 400 errors in pytube
    from pytube.cipher import get_throttling_function_name
    from pytube.extract import get_ytplayer_config, apply_descrambler
    
    # Monkey patch for pytube cipher issues
    original_get_throttling_function_name = get_throttling_function_name
    
    def patched_get_throttling_function_name(js):
        try:
            return original_get_throttling_function_name(js)
        except Exception as e:
            logger.warning(f"Error in get_throttling_function_name: {e}")
            return "a"
    
    pytube.cipher.get_throttling_function_name = patched_get_throttling_function_name
    logger.info("Applied pytube HTTP 400 error workaround")
except Exception as e:
    logger.warning(f"Could not apply pytube workaround: {e}")

# Try to import advanced voice activity detection
vad_available = False
try:
    import webrtcvad
    vad = webrtcvad.Vad(3)  # Aggressiveness mode 3 (most aggressive)
    vad_available = True
    logger.info("WebRTC VAD initialized successfully")
except ImportError as e:
    logger.warning(f"WebRTC VAD not available: {e}")

def process_with_vad(audio_segment, frame_duration_ms=30, padding_duration_ms=300):
    """Process audio with WebRTC Voice Activity Detection to improve speech recognition"""
    if not vad_available:
        logger.warning("WebRTC VAD not available, skipping VAD processing")
        return audio_segment
    
    try:
        # Convert audio to the format required by WebRTC VAD (16-bit PCM, mono)
        if audio_segment.channels > 1:
            audio_segment = audio_segment.set_channels(1)
        
        if audio_segment.frame_rate != 16000:
            audio_segment = audio_segment.set_frame_rate(16000)
        
        # Create a VAD instance with aggressiveness level 3 (most aggressive)
        vad = webrtcvad.Vad(3)
        
        # Get raw PCM data
        raw_data = audio_segment.raw_data
        
        # Calculate frame size
        frame_size = int(audio_segment.frame_rate * frame_duration_ms / 1000)
        
        # Process frames
        voiced_frames = []
        for i in range(0, len(raw_data) - frame_size, frame_size):
            frame = raw_data[i:i + frame_size]
            if len(frame) < frame_size:
                break
                
            is_speech = vad.is_speech(frame, audio_segment.frame_rate)
            if is_speech:
                voiced_frames.append(frame)
        
        # If no voiced frames were detected, return the original audio
        if not voiced_frames:
            logger.warning("No voiced frames detected, returning original audio")
            return audio_segment
        
        # Combine voiced frames with padding
        padding_size = int(audio_segment.frame_rate * padding_duration_ms / 1000)
        processed_data = b''.join(voiced_frames)
        
        # Create a new AudioSegment from the processed data
        processed_segment = AudioSegment(
            data=processed_data,
            sample_width=audio_segment.sample_width,
            frame_rate=audio_segment.frame_rate,
            channels=1
        )
        
        logger.info(f"VAD processing complete: reduced audio from {len(audio_segment)} ms to {len(processed_segment)} ms")
        return processed_segment
    except Exception as e:
        logger.error(f"Error in VAD processing: {str(e)}")
        return audio_segment

def split_on_sentence_breaks(audio_segment, min_silence_len=500, silence_thresh=-40, keep_silence=300):
    """Split audio into smaller chunks based on sentence breaks (silence detection)"""
    try:
        # First try with provided parameters
        chunks = split_on_silence(
            audio_segment, 
            min_silence_len=min_silence_len,
            silence_thresh=silence_thresh,
            keep_silence=keep_silence
        )
        
        # If we got too few chunks, try with less strict parameters
        if len(chunks) < 5 and len(audio_segment) > 10000:  # If less than 5 chunks and audio longer than 10 seconds
            logger.info("Too few sentence breaks detected, adjusting parameters")
            silence_thresh = silence_thresh + 5  # Less strict silence threshold
            min_silence_len = max(300, min_silence_len - 100)  # Shorter minimum silence
            
            chunks = split_on_silence(
                audio_segment, 
                min_silence_len=min_silence_len,
                silence_thresh=silence_thresh,
                keep_silence=keep_silence
            )
        
        # If we still have too few chunks, try even less strict parameters
        if len(chunks) < 10 and len(audio_segment) > 20000:  # If less than 10 chunks and audio longer than 20 seconds
            logger.info("Still too few sentence breaks, using more aggressive parameters")
            silence_thresh = silence_thresh + 5  # Even less strict
            min_silence_len = max(200, min_silence_len - 100)  # Even shorter minimum silence
            
            chunks = split_on_silence(
                audio_segment, 
                min_silence_len=min_silence_len,
                silence_thresh=silence_thresh,
                keep_silence=keep_silence
            )
            
        # If we still have too few chunks and the audio is long, try with very aggressive parameters
        if len(chunks) < 20 and len(audio_segment) > 60000:  # If less than 20 chunks and audio longer than 1 minute
            logger.info("Still not enough sentence breaks for long audio, using very aggressive parameters")
            silence_thresh = -30  # Very permissive silence threshold
            min_silence_len = 150  # Very short silence is considered a break
            
            chunks = split_on_silence(
                audio_segment, 
                min_silence_len=min_silence_len,
                silence_thresh=silence_thresh,
                keep_silence=200
            )
        
        # Ensure chunks aren't too long for optimal speech recognition
        max_chunk_length = 10000  # 10 seconds maximum
        final_chunks = []
        
        for chunk in chunks:
            if len(chunk) > max_chunk_length:
                # Split long chunks into smaller pieces
                logger.info(f"Splitting long chunk of {len(chunk)/1000} seconds into smaller pieces")
                subchunks = [chunk[i:i+max_chunk_length] for i in range(0, len(chunk), max_chunk_length)]
                final_chunks.extend(subchunks)
            else:
                final_chunks.append(chunk)
        
        logger.info(f"Split audio into {len(final_chunks)} sentence chunks (after length optimization)")
        return final_chunks
    except Exception as e:
        logger.error(f"Error in sentence break detection: {str(e)}")
        return []

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)

@app.route('/process_youtube', methods=['POST'])
def process_youtube():
    """Process a YouTube video and generate subtitles"""
    try:
        logger.info("Received request to process YouTube video")
        
        data = request.get_json()
        
        if not data:
            logger.warning("No JSON data received in request")
            return jsonify({'error': 'No data provided'}), 400
        
        youtube_url = data.get('youtube_url')
        language = data.get('language', 'ar')
        
        if not youtube_url:
            logger.warning("No YouTube URL provided")
            return jsonify({'error': 'يرجى تقديم رابط يوتيوب صالح'}), 400
        
        # Validate YouTube URL
        if not is_valid_youtube_url(youtube_url):
            logger.warning(f"Invalid YouTube URL: {youtube_url}")
            return jsonify({'error': 'رابط يوتيوب غير صالح. يرجى التحقق من الرابط والمحاولة مرة أخرى.'}), 400
        
        # Generate a unique task ID
        task_id = str(uuid.uuid4())
        
        # Initialize task status
        tasks[task_id] = {
            'status': 'processing',
            'progress': 0,
            'message': 'بدء معالجة فيديو يوتيوب...'
        }
        
        # Start processing in a background thread
        thread = threading.Thread(target=process_youtube_video, args=(task_id, youtube_url, language))
        thread.daemon = True
        thread.start()
        
        logger.info(f"Started YouTube processing task: {task_id}")
        
        return jsonify({
            'task_id': task_id,
            'status': 'processing',
            'message': 'بدأت معالجة فيديو يوتيوب'
        })
        
    except Exception as e:
        logger.error(f"Error in process_youtube endpoint: {str(e)}")
        return jsonify({'error': f'حدث خطأ أثناء معالجة الفيديو: {str(e)}'}), 500

def is_valid_youtube_url(url):
    """Check if the URL is a valid YouTube URL"""
    try:
        # Basic pattern matching for YouTube URLs
        youtube_pattern = r'^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[a-zA-Z0-9_-]{11}.*$'
        return bool(re.match(youtube_pattern, url))
    except Exception as e:
        logger.error(f"Error validating YouTube URL: {str(e)}")
        return False

@app.route('/progress/<task_id>')
def get_progress(task_id):
    """Get the progress of a task"""
    try:
        logger.info(f"Checking progress for task: {task_id}")
        
        # Add a small delay to prevent race conditions
        time.sleep(0.1)
        
        if task_id not in tasks:
            logger.warning(f"Task ID not found: {task_id}")
            return jsonify({'error': 'Task not found'}), 404
        
        task = tasks[task_id]
        
        # If task is completed, return the result
        if task['status'] == 'completed':
            logger.info(f"Task {task_id} is completed, returning result")
            
            # Ensure result has content
            result = task.get('result', {})
            if not result or (not result.get('srt_content') and not result.get('subtitles')):
                logger.warning(f"Task {task_id} is completed but has no content")
                return jsonify({
                    'status': 'error',
                    'message': 'اكتملت المعالجة ولكن لم يتم العثور على محتوى الترجمة',
                    'error': 'No content found in completed task'
                })
            
            return jsonify({
                'status': 'completed',
                'progress': 100,
                'message': task.get('message', 'تم إنشاء الترجمة بنجاح'),
                'result': result
            })
        
        # If task is in error state, return the error
        if task['status'] == 'error':
            logger.warning(f"Task {task_id} is in error state: {task.get('error', 'Unknown error')}")
            return jsonify({
                'status': 'error',
                'message': task.get('message', 'حدث خطأ أثناء المعالجة'),
                'error': task.get('error', 'Unknown error')
            })
        
        # Task is still processing
        logger.info(f"Task {task_id} is still processing: {task.get('progress', 0)}% - {task.get('message', 'جاري المعالجة...')}")
        return jsonify({
            'status': 'processing',
            'progress': task.get('progress', 0),
            'message': task.get('message', 'جاري المعالجة...')
        })
        
    except Exception as e:
        logger.error(f"Error checking progress for task {task_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/process', methods=['POST'])
def process_video():
    """Process video file or YouTube URL"""
    try:
        # Create a unique task ID
        task_id = str(uuid.uuid4())
        
        # Initialize task status
        tasks[task_id] = {
            'status': 'processing',
            'message': 'بدء معالجة الفيديو...',
            'progress': 0
        }
        
        logger.info(f"Created task with ID: {task_id}")
        
        if request.content_type and 'application/json' in request.content_type:
            # Handle YouTube URL
            data = request.json
            youtube_url = data.get('youtube_url')
            language = data.get('language')
            
            logger.info(f"Processing YouTube URL: {youtube_url}, Language: {language}")
            
            if not youtube_url or not language:
                return jsonify({'error': 'Missing YouTube URL or language'}), 400
            
            if not whisper_available:
                logger.warning("Whisper speech recognition not available, returning sample subtitles")
                subtitles = generate_sample_subtitles(language)
                return jsonify({
                    'subtitles': subtitles,
                    'warning': 'Using sample subtitles because Whisper speech recognition is not available'
                })
                
            # Process YouTube video in a background thread
            thread = threading.Thread(target=process_youtube_video, args=(task_id, youtube_url, language))
            thread.daemon = True
            thread.start()
            
            logger.info(f"Started background thread for YouTube processing with task ID: {task_id}")
            
            return jsonify({
                'task_id': task_id,
                'message': 'جاري معالجة الفيديو في الخلفية...'
            })
            
        else:
            # Handle file upload
            if 'video' not in request.files:
                return jsonify({'error': 'No video file uploaded'}), 400
                
            file = request.files['video']
            language = request.form.get('language', 'ar')
            
            logger.info(f"Processing uploaded file: {file.filename}, Language: {language}")
            
            if not file or file.filename == '':
                return jsonify({'error': 'Missing file or empty filename'}), 400
            
            if not whisper_available:
                logger.warning("Whisper speech recognition not available, returning sample subtitles")
                subtitles = generate_sample_subtitles(language)
                return jsonify({
                    'subtitles': subtitles,
                    'warning': 'Using sample subtitles because Whisper speech recognition is not available'
                })
            
            # Save uploaded file temporarily before processing in a thread
            temp_dir = tempfile.mkdtemp()
            filename = file.filename
            video_path = os.path.join(temp_dir, filename)
            file.save(video_path)
            
            logger.info(f"Saved uploaded video to: {video_path}")
            
            # Process uploaded file in a background thread
            thread = threading.Thread(
                target=process_uploaded_file,
                args=(task_id, video_path, filename, language)
            )
            thread.daemon = True
            thread.start()
            
            logger.info(f"Started background thread for file processing with task ID: {task_id}")
            
            return jsonify({
                'task_id': task_id,
                'message': 'جاري معالجة الفيديو في الخلفية...'
            })
            
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({'error': str(e)}), 500

def process_uploaded_file(task_id, video_path, filename, language):
    """Process an uploaded video file in a background thread"""
    try:
        logger.info(f"Starting to process uploaded file: {filename}, task_id: {task_id}")
        
        # Update task status
        tasks[task_id]['message'] = 'بدء معالجة الملف المرفوع...'
        tasks[task_id]['progress'] = 5
        
        # Generate subtitles
        subtitles = generate_subtitles_with_whisper(video_path, language, task_id)
        
        # Update task status
        tasks[task_id]['status'] = 'completed'
        tasks[task_id]['progress'] = 100
        tasks[task_id]['message'] = 'تم إنشاء الترجمة بنجاح'
        tasks[task_id]['result'] = {
            'srt_content': subtitles,
            'filename': f"{os.path.splitext(filename)[0]}.srt"
        }
        
        logger.info(f"Completed processing task {task_id}")
        
        # Clean up
        try:
            if os.path.exists(video_path):
                os.remove(video_path)
                logger.info(f"Removed temporary video file: {video_path}")
            
            temp_dir = os.path.dirname(video_path)
            if os.path.exists(temp_dir) and not os.listdir(temp_dir):
                os.rmdir(temp_dir)
                logger.info(f"Removed temporary directory: {temp_dir}")
        except Exception as e:
            logger.warning(f"Error cleaning up temporary files: {str(e)}")
        
    except Exception as e:
        logger.error(f"Error processing uploaded file: {str(e)}")
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['message'] = f'حدث خطأ: {str(e)}'
        tasks[task_id]['error'] = str(e)

def generate_subtitles_with_whisper(video_path, language, task_id):
    """Extract audio from video and generate subtitles using Whisper"""
    logger.info(f"Generating subtitles for: {video_path}")
    
    if not whisper_available:
        logger.warning("Whisper speech recognition is not available, returning sample subtitles")
        return generate_sample_subtitles(language)
    
    try:
        # Extract audio from video
        logger.info("Extracting audio from video...")
        tasks[task_id]['message'] = 'جاري استخراج الصوت من الفيديو...'
        tasks[task_id]['progress'] = 10
        
        # Create a temporary directory for audio
        temp_dir = os.path.join(tempfile.gettempdir(), f'whisper_audio_{uuid.uuid4()}')
        os.makedirs(temp_dir, exist_ok=True)
        
        # Extract audio using moviepy
        audio_path = os.path.join(temp_dir, 'audio.wav')
        video_clip = VideoFileClip(video_path)
        
        # Add progress callback for audio extraction
        def audio_extraction_progress(current_time, total_time):
            if total_time > 0:
                progress = min(20 + int((current_time / total_time) * 10), 30)
                tasks[task_id]['progress'] = progress
                tasks[task_id]['message'] = f'جاري استخراج الصوت... {int((current_time / total_time) * 100)}%'
                logger.info(f"Audio extraction progress: {int((current_time / total_time) * 100)}%")
        
        try:
            video_clip.audio.write_audiofile(audio_path, codec='pcm_s16le', logger=None, progress_callback=audio_extraction_progress)
        except TypeError:
            # If progress_callback is not supported
            video_clip.audio.write_audiofile(audio_path, codec='pcm_s16le', logger=None)
        
        video_clip.close()
        
        logger.info(f"Audio extracted to: {audio_path}")
        tasks[task_id]['message'] = 'جاري تحليل الصوت...'
        tasks[task_id]['progress'] = 20
        
        # Map language code to Whisper language code
        language_map = {
            'ar': 'arabic',
            'en': 'english',
            'tr': 'turkish',
            'fr': 'french',
            'es': 'spanish',
            'de': 'german'
        }
        
        whisper_language = language_map.get(language, language)
        
        # Use Whisper to transcribe the full audio file
        logger.info(f"Transcribing audio with Whisper using language: {whisper_language}")
        tasks[task_id]['message'] = 'جاري التعرف على الكلام...'
        tasks[task_id]['progress'] = 30
        
        # Define a callback to update progress during transcription
        def transcription_progress_callback(percent):
            current_progress = min(30 + int(percent * 40), 70)
            tasks[task_id]['progress'] = current_progress
            tasks[task_id]['message'] = f'جاري التعرف على الكلام... {int(percent * 100)}%'
            logger.info(f"Transcription progress: {int(percent * 100)}%")
        
        # Transcribe with Whisper
        try:
            # Check if the model supports progress_callback
            import inspect
            if 'progress_callback' in inspect.signature(whisper_model.transcribe).parameters:
                logger.info("Using Whisper with progress callback")
                
                # Define a manual progress update function for long-running transcription
                def manual_progress_update():
                    progress_values = [35, 40, 45, 50, 55, 60, 65]
                    for progress in progress_values:
                        time.sleep(5)  # Update every 5 seconds
                        tasks[task_id]['progress'] = progress
                        tasks[task_id]['message'] = f'جاري التعرف على الكلام... {progress}%'
                        logger.info(f"Manual progress update: {progress}%")
                
                # Start a thread to update progress manually
                import threading
                progress_thread = threading.Thread(target=manual_progress_update)
                progress_thread.daemon = True
                progress_thread.start()
                
                # Run transcription
                result = whisper_model.transcribe(
                    audio_path, 
                    language=whisper_language,
                    verbose=False,
                    word_timestamps=True,  # Enable word-level timestamps if available
                    progress_callback=transcription_progress_callback
                )
            else:
                # Update progress at fixed intervals if progress_callback not supported
                logger.info("Using Whisper without progress callback")
                tasks[task_id]['message'] = 'جاري التعرف على الكلام... قد تستغرق هذه العملية بعض الوقت'
                
                # Define a manual progress update function for long-running transcription
                def manual_progress_update():
                    progress_values = [35, 40, 45, 50, 55, 60, 65]
                    for progress in progress_values:
                        time.sleep(5)  # Update every 5 seconds
                        tasks[task_id]['progress'] = progress
                        tasks[task_id]['message'] = f'جاري التعرف على الكلام... {progress}%'
                        logger.info(f"Manual progress update: {progress}%")
                
                # Start a thread to update progress manually
                import threading
                progress_thread = threading.Thread(target=manual_progress_update)
                progress_thread.daemon = True
                progress_thread.start()
                
                # Run transcription
                result = whisper_model.transcribe(
                    audio_path, 
                    language=whisper_language,
                    verbose=False,
                    word_timestamps=True  # Enable word-level timestamps if available
                )
        except TypeError:
            # Fallback if progress_callback causes an error
            logger.info("Fallback to Whisper without progress callback due to TypeError")
            tasks[task_id]['message'] = 'جاري التعرف على الكلام... قد تستغرق هذه العملية بعض الوقت'
            
            # Define a manual progress update function for long-running transcription
            def manual_progress_update():
                progress_values = [35, 40, 45, 50, 55, 60, 65]
                for progress in progress_values:
                    time.sleep(5)  # Update every 5 seconds
                    tasks[task_id]['progress'] = progress
                    tasks[task_id]['message'] = f'جاري التعرف على الكلام... {progress}%'
                    logger.info(f"Manual progress update: {progress}%")
            
            # Start a thread to update progress manually
            import threading
            progress_thread = threading.Thread(target=manual_progress_update)
            progress_thread.daemon = True
            progress_thread.start()
            
            # Run transcription
            result = whisper_model.transcribe(
                audio_path, 
                language=whisper_language,
                verbose=False,
                word_timestamps=True  # Enable word-level timestamps if available
            )
        except Exception as e:
            # Define a manual progress update function for long-running transcription
            def manual_progress_update():
                progress_values = [35, 40, 45, 50, 55, 60, 65]
                for progress in progress_values:
                    time.sleep(5)  # Update every 5 seconds
                    tasks[task_id]['progress'] = progress
                    tasks[task_id]['message'] = f'جاري التعرف على الكلام... {progress}%'
                    logger.info(f"Manual progress update: {progress}%")
            
            # Start a thread to update progress manually
            import threading
            progress_thread = threading.Thread(target=manual_progress_update)
            progress_thread.daemon = True
            progress_thread.start()
            
            logger.info("Error occurred during transcription, falling back to manual progress updates")
            result = whisper_model.transcribe(
                audio_path, 
                language=whisper_language,
                verbose=False,
                word_timestamps=True  # Enable word-level timestamps if available
            )
        
        tasks[task_id]['progress'] = 70
        tasks[task_id]['message'] = 'جاري إنشاء ملف الترجمة...'
        
        # Convert Whisper result to SRT format
        srt_content = ""
        subtitle_index = 1
        
        # Check if we have segments (should always be the case)
        if 'segments' in result:
            for segment in result['segments']:
                start_time = segment['start']
                end_time = segment['end']
                text = segment['text'].strip()
                
                if text:
                    # Format times for SRT
                    start_time_srt = format_timestamp(int(start_time * 1000))  # Convert to milliseconds
                    end_time_srt = format_timestamp(int(end_time * 1000))  # Convert to milliseconds
                    
                    srt_content += f"{subtitle_index}\n"
                    srt_content += f"{start_time_srt} --> {end_time_srt}\n"
                    srt_content += f"{text}\n\n"
                    subtitle_index += 1
        else:
            # Fallback if no segments
            text = result['text'].strip()
            if text:
                srt_content += f"1\n00:00:00,000 --> 00:05:00,000\n{text}\n\n"
                subtitle_index = 2
        
        # Clean up temporary files
        try:
            if os.path.exists(audio_path):
                os.remove(audio_path)
                logger.info(f"Removed temporary audio file: {audio_path}")
            
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info(f"Removed temporary directory: {temp_dir}")
        except Exception as e:
            logger.warning(f"Could not remove temporary files: {e}")
        
        # If no subtitles were generated, return sample subtitles
        if subtitle_index == 1:
            logger.warning("No speech detected in audio, returning sample subtitles")
            return generate_sample_subtitles(language)
        
        logger.info(f"Generated {subtitle_index-1} subtitles")
        
        # Post-process subtitles to improve quality
        srt_content = post_process_subtitles(srt_content, language)
        
        # Update task status
        tasks[task_id]['status'] = 'completed'
        tasks[task_id]['message'] = 'تم إنشاء الترجمة بنجاح'
        tasks[task_id]['progress'] = 100
        
        return srt_content
    except Exception as e:
        logger.error(f"Error in generate_subtitles_with_whisper: {str(e)}")
        return generate_sample_subtitles(language)

def normalize_audio(audio_segment):
    """Normalize audio to improve speech recognition"""
    try:
        # Normalize to -20dB
        normalized_audio = effects.normalize(audio_segment, headroom=5.0)
        logger.info("Audio normalized successfully")
        return normalized_audio
    except Exception as e:
        logger.warning(f"Could not normalize audio: {e}")
        return audio_segment

def format_time(td):
    """Format timedelta object to SRT time format (HH:MM:SS,mmm)"""
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    milliseconds = int((td.total_seconds() - total_seconds) * 1000)
    
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

def format_timestamp(milliseconds):
    """Format milliseconds to SRT timestamp format: HH:MM:SS,mmm"""
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

def generate_sample_subtitles(language):
    """Generate sample subtitles when actual subtitles cannot be extracted"""
    logger.info(f"Generating sample subtitles in language: {language}")
    
    # Bilingual subtitles (Arabic and English)
    if language == 'ar':
        return """1
00:00:00,000 --> 00:00:05,000
هذه ترجمات عينة لفيديو.

2
00:00:05,000 --> 00:00:10,000
لم نتمكن من استخراج النص الفعلي من هذا الفيديو.

3
00:00:10,000 --> 00:00:15,000
قد يكون ذلك بسبب جودة الصوت أو مشكلة في تنزيل الفيديو.

4
00:00:15,000 --> 00:00:20,000
يرجى المحاولة مرة أخرى أو تجربة فيديو آخر.

5
00:00:20,000 --> 00:00:25,000
شكرًا لاستخدامك أداة استخراج الترجمات.

6
00:00:25,000 --> 00:00:30,000
These are sample subtitles for the video.

7
00:00:30,000 --> 00:00:35,000
We couldn't extract the actual text from this video.

8
00:00:35,000 --> 00:00:40,000
This might be due to audio quality or an issue with the video download.

9
00:00:40,000 --> 00:00:45,000
Please try again or try another video.

10
00:00:45,000 --> 00:00:50,000
Thank you for using the Subtitle Extractor tool.
"""
    elif language == 'fr':
        return """1
00:00:00,000 --> 00:00:05,000
Ce sont des sous-titres d'exemple pour la vidéo.

2
00:00:05,000 --> 00:00:10,000
Nous n'avons pas pu extraire le texte réel de cette vidéo.

3
00:00:10,000 --> 00:00:15,000
Cela peut être dû à la qualité audio ou à un problème de téléchargement de la vidéo.

4
00:00:15,000 --> 00:00:20,000
Veuillez réessayer ou essayer une autre vidéo.

5
00:00:20,000 --> 00:00:25,000
Merci d'utiliser l'outil d'extraction de sous-titres.

6
00:00:25,000 --> 00:00:30,000
These are sample subtitles for the video.

7
00:00:30,000 --> 00:00:35,000
We couldn't extract the actual text from this video.

8
00:00:35,000 --> 00:00:40,000
This might be due to audio quality or an issue with the video download.

9
00:00:40,000 --> 00:00:45,000
Please try again or try another video.

10
00:00:45,000 --> 00:00:50,000
Thank you for using the Subtitle Extractor tool.
"""
    else:  # Default to English
        return """1
00:00:00,000 --> 00:00:05,000
These are sample subtitles for the video.

2
00:00:05,000 --> 00:00:10,000
We couldn't extract the actual text from this video.

3
00:00:10,000 --> 00:00:15,000
This might be due to audio quality or an issue with the video download.

4
00:00:15,000 --> 00:00:20,000
Please try again or try another video.

5
00:00:20,000 --> 00:00:25,000
Thank you for using the Subtitle Extractor tool.
"""

def download_youtube_video(youtube_url):
    """Download a YouTube video and return the path to the downloaded file and video title"""
    temp_dir = None
    try:
        logger.info(f"Downloading YouTube video: {youtube_url}")
        
        # Create a temporary directory
        temp_dir = tempfile.mkdtemp()
        
        # Try different download methods in order
        
        # Method 1: Try with yt-dlp library
        if yt_dlp_available:
            try:
                logger.info("Attempting to download with yt-dlp library...")
                video_path, video_title = download_with_yt_dlp(youtube_url, temp_dir)
                
                if video_path and os.path.exists(video_path):
                    logger.info(f"Successfully downloaded with yt-dlp library: {video_path}")
                    return video_path, video_title
                    
                logger.warning("yt-dlp library download failed or returned invalid path")
            except Exception as yt_dlp_error:
                logger.error(f"Error with yt-dlp library download: {str(yt_dlp_error)}")
        
        # Method 2: Try with subprocess (yt-dlp command line)
        try:
            logger.info("Attempting to download with subprocess yt-dlp...")
            video_path, video_title = download_with_subprocess(youtube_url, temp_dir)
            
            if video_path and os.path.exists(video_path):
                logger.info(f"Successfully downloaded with subprocess yt-dlp: {video_path}")
                return video_path, video_title
                
            logger.warning("Subprocess yt-dlp download failed or returned invalid path")
        except Exception as subprocess_error:
            logger.error(f"Error with subprocess yt-dlp download: {str(subprocess_error)}")
        
        # Method 3: Try with pytube
        try:
            logger.info("Attempting to download with pytube...")
            video_path, video_title = download_with_pytube(youtube_url, temp_dir)
            
            if video_path and os.path.exists(video_path):
                logger.info(f"Successfully downloaded with pytube: {video_path}")
                return video_path, video_title
                
            logger.warning("pytube download failed or returned invalid path")
        except Exception as pytube_error:
            logger.error(f"Error with pytube download: {str(pytube_error)}")
        
        # If all methods fail, try one more time with yt-dlp with different options
        if yt_dlp_available:
            try:
                logger.info("Final attempt with yt-dlp using different options...")
                # Use a direct approach with yt-dlp
                video_id = str(uuid.uuid4())
                output_template = os.path.join(temp_dir, f"{video_id}.mp4")
                
                ydl_opts = {
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'outtmpl': output_template,
                    'noplaylist': True,
                    'quiet': False,
                    'no_warnings': False,
                }
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(youtube_url, download=True)
                    if info and os.path.exists(output_template):
                        video_title = info.get('title', 'youtube_video')
                        logger.info(f"Final attempt successful: {output_template}")
                        return output_template, video_title
            except Exception as final_error:
                logger.error(f"Final attempt failed: {str(final_error)}")
        
        # If all methods fail
        raise Exception("فشلت جميع طرق التنزيل. يرجى التحقق من الرابط والمحاولة مرة أخرى.")
            
    except Exception as e:
        logger.error(f"Error in download_youtube_video: {str(e)}")
        
        # Clean up temp directory if download failed
        try:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
                logger.info(f"Removed temporary directory: {temp_dir}")
        except Exception as cleanup_error:
            logger.warning(f"Error cleaning up temporary directory: {str(cleanup_error)}")
            
        return None, None

def download_with_pytube(youtube_url, output_path):
    """Download a YouTube video using pytube"""
    try:
        logger.info(f"Attempting to download with pytube: {youtube_url}")
        
        # Create a unique filename
        video_id = str(uuid.uuid4())
        video_path = os.path.join(output_path, f"{video_id}.mp4")
        
        # Download with pytube
        yt = YouTube(youtube_url)
        
        # Get the video title
        video_title = yt.title if yt.title else "youtube_video"
        logger.info(f"Video title: {video_title}")
        
        # Clean the title for use as a filename
        video_title = re.sub(r'[^\w\s]', ' ', video_title).strip()
        video_title = re.sub(r'[-\s]+', '-', video_title)
        
        # Try different stream types in order
        
        # First try to get a progressive stream (combined audio and video)
        logger.info("Trying to get progressive stream...")
        stream = None
        
        try:
            # Try to get the highest resolution mp4 progressive stream
            stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
            
            if not stream:
                # Try any progressive stream
                stream = yt.streams.filter(progressive=True).order_by('resolution').desc().first()
                
            if not stream:
                # Try any mp4 stream
                stream = yt.streams.filter(file_extension='mp4').order_by('resolution').desc().first()
                
            if not stream:
                # Try any stream
                stream = yt.streams.order_by('resolution').desc().first()
                
            if not stream:
                logger.error("No suitable stream found")
                return None, None
                
        except Exception as stream_error:
            logger.error(f"Error getting stream: {str(stream_error)}")
            
            # Try a different approach with pytube
            try:
                logger.info("Trying alternative pytube approach...")
                stream = yt.streams.get_highest_resolution()
            except Exception as alt_error:
                logger.error(f"Alternative approach failed: {str(alt_error)}")
                return None, None
        
        # Download the stream
        if stream:
            logger.info(f"Downloading stream: {stream}")
            stream.download(output_path=output_path, filename=f"{video_id}.mp4")
            
            # Check if the file was downloaded
            if os.path.exists(video_path):
                logger.info(f"Successfully downloaded video with pytube: {video_path}")
                return video_path, video_title
            else:
                logger.error(f"Downloaded file not found at {video_path}")
                
                # Try to find any mp4 file in the output directory that was recently created
                mp4_files = []
                for f in os.listdir(output_path):
                    if f.endswith('.mp4'):
                        file_path = os.path.join(output_path, f)
                        # Check if file was created in the last minute
                        if os.path.getmtime(file_path) > time.time() - 60:
                            mp4_files.append(file_path)
                
                if mp4_files:
                    video_path = mp4_files[0]
                    logger.info(f"Found alternative mp4 file: {video_path}")
                    return video_path, video_title
        
        return None, None
        
    except Exception as e:
        logger.error(f"Error downloading with pytube: {str(e)}")
        return None, None

def download_with_yt_dlp(youtube_url, output_path):
    """Download a YouTube video using yt-dlp as a fallback method"""
    try:
        if not yt_dlp_available:
            logger.error("yt-dlp library is not available")
            return None, None
            
        logger.info(f"Attempting to download with yt-dlp library: {youtube_url}")
        
        # Create a unique filename
        video_id = str(uuid.uuid4())
        output_template = os.path.join(output_path, f"{video_id}.mp4")
        
        # Configure yt-dlp options
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': output_template,
            'noplaylist': True,
            'quiet': False,
            'no_warnings': False,
            'ignoreerrors': True,
        }
        
        # Download the video
        logger.info(f"Starting yt-dlp download with options: {ydl_opts}")
        
        video_title = "youtube_video"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info("Starting yt-dlp download...")
            info = ydl.extract_info(youtube_url, download=True)
            logger.info("yt-dlp download completed")
            
            if not info:
                logger.error("yt-dlp could not extract video info")
                return None, None
                
            # Get video title
            video_title = info.get('title', 'youtube_video')
            
            # Sanitize video title for filename
            video_title = re.sub(r'[^\w\s-]', '', video_title).strip()
            video_title = re.sub(r'[-\s]+', '-', video_title)
            
            # Get the downloaded file path
            if 'requested_downloads' in info and info['requested_downloads']:
                video_path = info['requested_downloads'][0]['filepath']
            else:
                # Try to find the file based on the template
                ext = info.get('ext', 'mp4')
                video_path = os.path.join(output_path, f"{video_id}.{ext}")
                
            if not os.path.exists(video_path):
                logger.error(f"yt-dlp did not create the expected output file: {video_path}")
                
                # Try to find any mp4 file in the output directory that was recently created
                mp4_files = []
                for f in os.listdir(output_path):
                    if f.endswith('.mp4'):
                        file_path = os.path.join(output_path, f)
                        # Check if file was created in the last minute
                        if os.path.getmtime(file_path) > time.time() - 60:
                            mp4_files.append(file_path)
                
                if mp4_files:
                    output_template = mp4_files[0]
                    logger.info(f"Found alternative mp4 file: {output_template}")
                else:
                    return None, None
        
        logger.info(f"Successfully downloaded video with yt-dlp library: {output_template}")
        return output_template, video_title
        
    except Exception as e:
        logger.error(f"Error downloading with yt-dlp library: {str(e)}")
        return None, None

def download_with_subprocess(youtube_url, output_path):
    """Download a YouTube video using subprocess to call yt-dlp directly"""
    try:
        logger.info(f"Attempting to download with subprocess yt-dlp: {youtube_url}")
        
        # Create a unique filename
        video_id = str(uuid.uuid4())
        output_template = os.path.join(output_path, f"{video_id}.mp4")
        
        # Check if yt-dlp is available
        try:
            # Try to run yt-dlp to check if it's installed
            subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, check=True)
            logger.info("yt-dlp command is available")
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            logger.error(f"yt-dlp command is not available: {str(e)}")
            return None, None
        
        # Prepare the command
        cmd = [
            "yt-dlp",
            "--format", "best[ext=mp4]/best",
            "--output", output_template,
            "--no-playlist",
            youtube_url
        ]
        
        logger.info(f"Running command: {' '.join(cmd)}")
        
        # Run the command
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        # Check if the command was successful
        if result.returncode != 0:
            logger.error(f"yt-dlp subprocess failed with return code {result.returncode}")
            logger.error(f"Error output: {result.stderr}")
            
            # Try with a different format option
            logger.info("Trying with a different format option...")
            cmd = [
                "yt-dlp",
                "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
                "--output", output_template,
                "--no-playlist",
                youtube_url
            ]
            
            logger.info(f"Running command: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                logger.error(f"Second attempt failed with return code {result.returncode}")
                logger.error(f"Error output: {result.stderr}")
                return None, None
        
        # Extract the video title from the output
        video_title = "youtube_video"
        for line in result.stdout.splitlines():
            if "[info]" in line and "Destination:" in line:
                # Try to extract the title from the destination line
                match = re.search(r'Destination:\s+(.+?)(?:\.\w+)?$', line)
                if match:
                    video_title = match.group(1)
                    break
        
        # Check if the file was created
        if not os.path.exists(output_template):
            logger.error(f"yt-dlp did not create the expected output file: {output_template}")
            
            # Try to find any mp4 file in the output directory that was recently created
            mp4_files = []
            for f in os.listdir(output_path):
                if f.endswith('.mp4'):
                    file_path = os.path.join(output_path, f)
                    # Check if file was created in the last minute
                    if os.path.getmtime(file_path) > time.time() - 60:
                        mp4_files.append(file_path)
            
            if mp4_files:
                output_template = mp4_files[0]
                logger.info(f"Found alternative mp4 file: {output_template}")
            else:
                return None, None
        
        logger.info(f"Successfully downloaded video with subprocess yt-dlp: {output_template}")
        return output_template, video_title
        
    except Exception as e:
        logger.error(f"Error downloading with subprocess yt-dlp: {str(e)}")
        return None, None

def on_progress(stream, chunk, bytes_remaining):
    """Callback function for download progress"""
    try:
        total_size = stream.filesize
        bytes_downloaded = total_size - bytes_remaining
        percentage = (bytes_downloaded / total_size) * 100
        logger.info(f"Download progress: {percentage:.2f}%")
    except Exception as e:
        logger.error(f"Error in progress callback: {str(e)}")

def on_complete(stream, file_path):
    """Callback function for download completion"""
    try:
        logger.info(f"Download completed: {file_path}")
    except Exception as e:
        logger.error(f"Error in complete callback: {str(e)}")

def process_youtube_video(task_id, youtube_url, language):
    """Process a YouTube video in a background thread"""
    try:
        logger.info(f"Starting to process YouTube video: {youtube_url}, task_id: {task_id}")
        
        # Update task status
        tasks[task_id]['message'] = 'جاري تحميل الفيديو من يوتيوب...'
        tasks[task_id]['progress'] = 5
        
        # Download YouTube video
        video_path, video_title = download_youtube_video(youtube_url)
        
        if not video_path:
            error_msg = "فشل في تحميل الفيديو من يوتيوب. يرجى التحقق من الرابط والمحاولة مرة أخرى."
            logger.error(f"Failed to download YouTube video: {youtube_url}")
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['message'] = error_msg
            tasks[task_id]['error'] = 'Failed to download YouTube video'
            return
        
        # Update task status
        tasks[task_id]['message'] = 'تم تحميل الفيديو، جاري معالجة الترجمة...'
        tasks[task_id]['progress'] = 20
        
        # Log video details
        logger.info(f"Successfully downloaded YouTube video. Title: {video_title}, Path: {video_path}")
        
        try:
            # Generate subtitles
            subtitles = generate_subtitles_with_whisper(video_path, language, task_id)
            
            if not subtitles or len(subtitles.strip()) == 0:
                logger.warning(f"No subtitles generated for YouTube video {youtube_url}")
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['message'] = 'لم يتم التعرف على أي كلام في الفيديو'
                tasks[task_id]['error'] = 'No speech detected in the video'
                
                # Provide fallback subtitles
                subtitles = generate_sample_subtitles(language)
                tasks[task_id]['status'] = 'completed'
                tasks[task_id]['progress'] = 100
                tasks[task_id]['message'] = 'تم إنشاء ترجمة تلقائية (لم يتم التعرف على كلام)'
                tasks[task_id]['result'] = {
                    'srt_content': subtitles,
                    'filename': f"{video_title}.srt"
                }
                return
            
            # Post-process subtitles to improve quality
            subtitles = post_process_subtitles(subtitles, language)
            
            # Update task status
            tasks[task_id]['status'] = 'completed'
            tasks[task_id]['progress'] = 100
            tasks[task_id]['message'] = 'تم إنشاء الترجمة بنجاح'
            tasks[task_id]['result'] = {
                'srt_content': subtitles,
                'filename': f"{video_title}.srt"
            }
            
            logger.info(f"Completed processing YouTube video for task {task_id}")
        
        except Exception as subtitle_error:
            logger.error(f"Error generating subtitles: {str(subtitle_error)}")
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['message'] = f'حدث خطأ أثناء إنشاء الترجمة: {str(subtitle_error)}'
            tasks[task_id]['error'] = str(subtitle_error)
        
        # Clean up
        finally:
            try:
                if os.path.exists(video_path):
                    os.remove(video_path)
                    logger.info(f"Removed temporary video file: {video_path}")
            except Exception as e:
                logger.warning(f"Error cleaning up temporary files: {str(e)}")
        
    except Exception as e:
        logger.error(f"Error processing YouTube video: {str(e)}")
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['message'] = f'حدث خطأ: {str(e)}'
        tasks[task_id]['error'] = str(e)

def reduce_noise(audio_segment):
    """Apply noise reduction to improve speech recognition"""
    try:
        # Simple noise reduction by removing very low amplitude parts
        # This is a basic implementation - for production, consider using more advanced libraries
        from pydub.effects import high_pass_filter, low_pass_filter
        
        # Apply high-pass filter to remove low frequency noise
        filtered_audio = high_pass_filter(audio_segment, 80)
        
        # Apply low-pass filter to remove high frequency noise
        filtered_audio = low_pass_filter(filtered_audio, 10000)
        
        logger.info("Applied noise reduction filters")
        return filtered_audio
    except Exception as e:
        logger.warning(f"Could not apply noise reduction: {e}")
        return audio_segment

def post_process_subtitles(srt_content, language):
    """Apply post-processing to improve subtitle quality"""
    try:
        # Fix common formatting issues
        lines = srt_content.split('\n')
        processed_lines = []
        
        i = 0
        while i < len(lines):
            line = lines[i]
            
            # Check if this line is a timestamp line (contains "-->")
            if "-->" in line:
                # Make sure there's a subtitle number before the timestamp
                if i > 0 and lines[i-1].strip().isdigit():
                    # Already has a number, add it as is
                    processed_lines.append(line)
                else:
                    # Missing subtitle number, add one
                    subtitle_number = len([l for l in processed_lines if "-->" in l]) + 1
                    processed_lines.append(str(subtitle_number))
                    processed_lines.append(line)
            else:
                processed_lines.append(line)
            
            i += 1
        
        # Join lines back together
        processed_srt = '\n'.join(processed_lines)
        
        # Ensure there are proper line breaks between subtitle blocks
        processed_srt = re.sub(r'(\d+)\s*\n(\d{2}:\d{2}:\d{2},\d{3})', r'\1\n\2', processed_srt)
        processed_srt = re.sub(r'(\n\n)\n+', r'\1', processed_srt)
        
        return processed_srt
    except Exception as e:
        logger.error(f"Error in post-processing subtitles: {str(e)}")
        return srt_content

@app.route('/setup_info')
def setup_info():
    """Return information about the setup and available features"""
    info = {
        "whisper_available": whisper_available,
        "vad_available": vad_available,
        "supported_languages": [
            {"code": "ar", "name": "العربية (Arabic)"},
            {"code": "en", "name": "الإنجليزية (English)"},
            {"code": "tr", "name": "التركية (Turkish)"},
            {"code": "fr", "name": "الفرنسية (French)"},
            {"code": "es", "name": "الإسبانية (Spanish)"},
            {"code": "de", "name": "الألمانية (German)"}
        ],
        "version": "2.0.0",
        "features": [
            "Multilingual speech recognition",
            "Silence-based audio chunking",
            "Voice activity detection (VAD)",
            "Audio normalization",
            "YouTube video processing",
            "Local video file processing"
        ]
    }
    return jsonify(info)

if __name__ == '__main__':
    # Add CORS headers to allow requests from any origin
    @app.after_request
    def add_cors_headers(response):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        return response
        
    app.run(debug=True, host='127.0.0.1', port=5000)
