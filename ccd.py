#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description='Run claude-code Docker container')
    parser.add_argument('command', choices=['build', 'run'], help='Command to execute')
    parser.add_argument('app_folder', nargs='?', default='./app', 
                       help='App folder path (default: ./app)')
    parser.add_argument('--home', default='./home', 
                       help='Home folder path (default: ./home)')
    
    args = parser.parse_args()
    
    IMAGE_NAME = 'claude-code'
    
    if args.command == 'build':
        build_image(IMAGE_NAME)
    elif args.command == 'run':
        run_container(IMAGE_NAME, args.app_folder, args.home)

def build_image(image_name):
    """Build the Docker image"""
    print(f"Building Docker image: {image_name}")
    try:
        subprocess.run(['docker', 'build', '-t', image_name, '.'], check=True)
        print("Build completed successfully!")
    except subprocess.CalledProcessError as e:
        print(f"Build failed with exit code {e.returncode}")
        sys.exit(1)

def run_container(image_name, app_folder, home_folder):
    """Run the Docker container with specified volumes"""
    # Convert to absolute paths
    app_path = Path(app_folder).resolve()
    home_path = Path(home_folder).resolve()
    
    # Create necessary directories and files
    app_path.mkdir(parents=True, exist_ok=True)
    claude_dir = home_path / '.claude'
    claude_dir.mkdir(parents=True, exist_ok=True)
    
    claude_json = home_path / '.claude.json'
    claude_json.touch(exist_ok=True)
    
    # Build Docker run command
    cmd = [
        'docker', 'run', '-it', '--rm',
        '-v', f'{app_path}:/app',
        '-v', f'{claude_dir}:/home/ubuntu/.claude',
        '-v', f'{claude_json}:/home/ubuntu/.claude.json',
        image_name
    ]
    
    print(f"Running container with:")
    print(f"  App folder: {app_path}")
    print(f"  Home folder: {home_path}")
    print(f"  Command: {' '.join(cmd)}")
    
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Container run failed with exit code {e.returncode}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nContainer stopped by user")

if __name__ == '__main__':
    main()
