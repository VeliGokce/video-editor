# Media Editor

A modern Windows video editor for MP4 files with both English and Turkish language support.

## Features

- Trim videos and remove sections
- Insert videos at the beginning, end, or a specific timestamp
- Image overlay support
- Horizontal and vertical encoding profiles
- CPU and GPU acceleration
- Persistent quick settings
- Pre-processing validation and safe cancellation
- Accurate keyframe validation with FFprobe
- Lossless trimming/merging when safe, full re-encoding only when required

## Building the EXE with GitHub

Every update pushed to the `main` branch automatically builds two standalone executables using GitHub Actions:

- `MediaEditor-Win10-Win11.exe`
- `MediaEditor-Win7.exe`

When a Git tag such as `v1.1.0` is pushed, both executables are automatically attached to the corresponding GitHub Release.

No installation of Python, Conda, or FFmpeg is required on the target computer.

## About

Media Editor is a lightweight Windows application designed for fast and reliable MP4 editing. It provides precise trimming, merging, overlays, customizable encoding profiles, hardware acceleration, and reusable quick settings while minimizing unnecessary re-encoding to preserve video quality.
