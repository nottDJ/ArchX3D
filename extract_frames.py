import os
import subprocess

# Your exact file paths
VIDEO_INPUT = r"C:\Users\ASUS\Downloads\A_seamless_continuous_camera_shot_starting_with_a__7fa8818302 (online-video-cutter.com) (1).mp4"
OUTPUT_DIR = r"D:\program\ArchX3D\output\sequence" 

def extract_frames(video_path, output_dir, fps=30):
    # Ensure the output folder exists
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    print(f"Extracting frames at {fps} FPS... Hang tight.")
    
    # FFmpeg command: Extracts at 30fps with high JPEG quality
    command = [
        "ffmpeg",
        "-i", video_path,
        "-vf", f"fps={fps}", 
        "-q:v", "2",
        os.path.join(output_dir, "frame_%04d.jpg")
    ]

    try:
        subprocess.run(command, check=True)
        print(f"✅ Success! 30fps frames extracted to: {output_dir}")
        print("Move this folder to your Next.js 'public' directory next.")
    except subprocess.CalledProcessError as e:
        print(f"❌ Error extracting frames: {e}")
    except FileNotFoundError:
        print("❌ FFmpeg not found. Please ensure FFmpeg is installed and in your PATH.")

if __name__ == "__main__":
    extract_frames(VIDEO_INPUT, OUTPUT_DIR)