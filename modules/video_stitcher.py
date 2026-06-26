import cv2
import os
import shutil
import sys

# Set up paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(BASE_DIR, 'output')
FRAMES_DIR = os.path.join(OUTPUT_DIR, 'frames')
VIDEO_OUTPUT_PATH = os.path.join(OUTPUT_DIR, 'walkthrough.mp4')

def stitch_video():
    print("--- Step 4: Video Stitching ---")
    
    if not os.path.exists(FRAMES_DIR):
        print(f"ERROR: Frames directory not found at {FRAMES_DIR}")
        sys.exit(1)
        
    # Get all PNG files and sort them numerically
    files = [f for f in os.listdir(FRAMES_DIR) if f.endswith('.png')]
    if not files:
        print(f"ERROR: No PNG frames found in {FRAMES_DIR}")
        sys.exit(1)
        
    # Sort files by the numeric part: 'frame_0001.png' -> 1
    files.sort(key=lambda x: int(x.split('_')[1].split('.')[0]))
    
    print(f"Found {len(files)} frames. Starting compilation...")
    
    # Read first frame to get dimensions
    first_frame_path = os.path.join(FRAMES_DIR, files[0])
    first_frame = cv2.imread(first_frame_path)
    if first_frame is None:
        print(f"ERROR: Could not read the first frame: {first_frame_path}")
        sys.exit(1)
        
    height, width, layers = first_frame.shape
    
    # Initialize VideoWriter
    # 'mp4v' is a common codec for MP4
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    video = cv2.VideoWriter(VIDEO_OUTPUT_PATH, fourcc, 30, (width, height))
    
    try:
        for i, filename in enumerate(files):
            frame_path = os.path.join(FRAMES_DIR, filename)
            frame = cv2.imread(frame_path)
            if frame is None:
                print(f"WARNING: Skipping corrupted frame: {frame_path}")
                continue
            
            video.write(frame)
            
            if (i + 1) % 10 == 0:
                print(f"Processed {i + 1}/{len(files)} frames...")
                
        print(f"SUCCESS: Video saved to {VIDEO_OUTPUT_PATH}")
        
        # Cleanup: Delete the frames directory to save space
        print(f"Cleaning up: Removing frames directory {FRAMES_DIR}")
        shutil.rmtree(FRAMES_DIR)
        
    except Exception as e:
        print(f"CRITICAL ERROR during video stitching: {e}")
        sys.exit(1)
    finally:
        video.release()

if __name__ == "__main__":
    stitch_video()
