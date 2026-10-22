"""Raspberry Pi FPC camera module.

This module provides a simple interface for camera operations
on Raspberry Pi using picamera2, including still image capture,
video recording, and streaming.

Tested on OV5647 sensor (Pi Camera v1.3) with capabilities:
  - Max resolution: 2592x1944 @ 15.63 fps
  - Default video: 1280x720 @ up to 30 fps
  - Encoders: H264, MJPEG

Classes
-------
Camera
    Manager for FPC camera capture and video operations.
"""

from pathlib import Path

try:
    from picamera2 import Picamera2
    from picamera2.encoders import (
        H264Encoder,
        MJPEGEncoder,
    )
except ImportError:
    # Allow module to be imported on non-RPi systems
    # (for testing/development)
    Picamera2 = None  # type: ignore
    H264Encoder = None  # type: ignore
    MJPEGEncoder = None  # type: ignore


class Camera:
    """Manager for Raspberry Pi FPC camera operations.

    Handles image capture, video recording, and streaming
    with configurable resolution and frame rate.

    Good default combinations for OV5647 sensor:
      - Still capture: 2592x1944 (max resolution)
      - Video: 1280x720 @ 30 fps (practical balance)
      - High-speed video: 1920x1080 @ 30 fps
      - Streaming: 640x480 @ 30 fps (lower bandwidth)

    Parameters
    ----------
    camera_index : int, optional
        Index of the camera to use (default: 0)
    width : int, optional
        Capture width in pixels (default: 1280)
    height : int, optional
        Capture height in pixels (default: 720)
    fps : int, optional
        Frames per second (default: 30)
    encoder : str, optional
        Video encoder to use: 'h264' or 'mjpeg'
        (default: 'h264')
    directory : str or Path, optional
        Directory for saving video recordings.
        If not provided, files are saved to current
        directory (default: None)
    photo_directory : str or Path, optional
        Directory for saving still image captures.
        Defaults to ``directory`` when not provided.
    """

    def __init__(
        self,
        camera_index: int = 0,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        encoder: str = "h264",
        directory: str | Path | None = None,
        photo_directory: str | Path | None = None,
    ) -> None:
        """Initialize the camera.

        Parameters
        ----------
        camera_index : int, optional
            Index of the camera to use (default: 0)
        width : int, optional
            Capture width in pixels (default: 1280)
        height : int, optional
            Capture height in pixels (default: 720)
        fps : int, optional
            Frames per second (default: 30)
        encoder : str, optional
            Video encoder: 'h264' or 'mjpeg'
            (default: 'h264')
        directory : str or Path, optional
            Directory for saving video recordings (default: None,
            saves to current directory)
        photo_directory : str or Path, optional
            Directory for saving still image captures.
            Defaults to ``directory`` when not provided.

        Raises
        ------
        RuntimeError
            If camera fails to initialize
        ValueError
            If encoder is not 'h264' or 'mjpeg'
        """
        if Picamera2 is None:
            raise RuntimeError(
                "picamera2 not available. "
                "This module requires a Raspberry Pi "
                "with picamera2 installed."
            )

        if encoder not in ("h264", "mjpeg"):
            raise ValueError(f"encoder must be 'h264' or 'mjpeg', " f"got '{encoder}'")

        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.fps = fps
        self.encoder = encoder
        self.directory = Path(directory) if directory else Path.cwd()
        self.photo_directory = Path(photo_directory) if photo_directory else self.directory
        self._camera = None
        self._is_recording = False

        try:
            self._camera = Picamera2(camera_index)
        except Exception as e:
            raise RuntimeError(f"Failed to initialize camera {camera_index}: {e}") from e

    def _validate_filename(self, filename: str) -> Path:
        """Validate that filename is a plain filename.

        Rejects any path-like components including
        absolute paths, relative traversal, or
        directory separators.

        Parameters
        ----------
        filename : str
            The filename to validate

        Returns
        -------
        Path
            The full path with directory prepended

        Raises
        ------
        ValueError
            If filename contains path separators or
            traversal components
        """
        # Reject absolute paths
        if Path(filename).is_absolute():
            raise ValueError(f"Filename cannot be absolute: {filename}")

        # Reject path separators
        if "/" in filename or "\\" in filename:
            raise ValueError(f"Filename cannot contain path separators: " f"{filename}")

        # Reject traversal attempts (.. or . as components)
        if filename == ".." or filename == ".":
            raise ValueError(f"Filename cannot be '{filename}'")

        # Reject filenames starting with . (hidden files)
        if filename.startswith("."):
            raise ValueError(f"Filename cannot start with '.': {filename}")

        return self.directory / filename

    def _validate_in(self, directory: Path, filename: str) -> Path:
        """Validate filename and resolve it against a given directory."""
        self._validate_filename(filename)
        return directory / filename

    def capture_image(self, filename: str) -> None:
        """Capture a still image and save to file.

        Parameters
        ----------
        filename : str
            Plain filename (without path separators).
            File will be saved to the ``photo_directory``
            specified at initialization.

        Raises
        ------
        ValueError
            If filename contains path separators
            or traversal components
        RuntimeError
            If camera is not initialized or
            capture fails
        """
        if self._camera is None:
            raise RuntimeError("Camera not initialized")

        self.photo_directory.mkdir(parents=True, exist_ok=True)
        # Validate filename and resolve against photo directory
        path = self._validate_in(self.photo_directory, filename)

        try:
            config = self._camera.create_still_configuration(
                main={"size": (self.width, self.height)}
            )
            self._camera.configure(config)
            self._camera.start()
            self._camera.capture_file(str(path))
            self._camera.stop()
        except ValueError:
            raise
        except Exception as e:
            raise RuntimeError(f"Image capture failed: {e}") from e

    def start_recording(self, filename: str) -> None:
        """Start recording video to a file.

        Parameters
        ----------
        filename : str
            Plain filename (without path separators).
            File will be saved to the ``directory``
            (video directory) specified at initialization.

        Raises
        ------
        ValueError
            If filename contains path separators
            or traversal components
        RuntimeError
            If camera is not initialized,
            already recording, or recording fails
        """
        if self._camera is None:
            raise RuntimeError("Camera not initialized")

        if self._is_recording:
            raise RuntimeError("Camera is already recording")

        self.directory.mkdir(parents=True, exist_ok=True)
        # Validate filename and resolve against video directory
        path = self._validate_in(self.directory, filename)

        try:
            config = self._camera.create_video_configuration(
                main={"size": (self.width, self.height)},
                encode="main",
            )
            self._camera.configure(config)
            self._camera.start()

            # Select encoder based on configuration
            if self.encoder == "h264":
                encoder_obj = H264Encoder(
                    bitrate=5000000,
                    framerate=self.fps,
                )
            else:  # mjpeg
                encoder_obj = MJPEGEncoder(
                    framerate=self.fps,
                )

            self._camera.start_recording(
                encoder_obj,
                output=str(path),
            )
            self._is_recording = True
        except ValueError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to start recording: {e}") from e

    def stop_recording(self) -> None:
        """Stop the current video recording.

        Raises
        ------
        RuntimeError
            If camera is not recording
        """
        if not self._is_recording:
            raise RuntimeError("Camera is not recording")

        if self._camera is None:
            raise RuntimeError("Camera not initialized")

        try:
            self._camera.stop_recording()
            self._camera.stop()
            self._is_recording = False
        except Exception as e:
            raise RuntimeError(f"Failed to stop recording: {e}") from e

    def get_frame_generator(self):
        """Get a generator that yields camera frames.

        Yields
        ------
        bytes
            Raw frame data from the camera buffer

        Raises
        ------
        RuntimeError
            If camera is not initialized or
            streaming fails

        Notes
        -----
        This generator runs indefinitely. Use in a
        loop and break when done. Frames are raw
        image data (not JPEG-encoded).

        For MJPEG streaming, use get_jpeg_frame_generator()
        instead, which yields JPEG-encoded frames.
        """
        if self._camera is None:
            raise RuntimeError("Camera not initialized")

        try:
            config = self._camera.create_video_configuration(
                main={"size": (self.width, self.height)},
            )
            self._camera.configure(config)
            self._camera.start()

            while True:
                frame = self._camera.capture_buffer("main")
                yield frame.tobytes()

        except Exception as e:
            raise RuntimeError(f"Streaming failed: {e}") from e
        finally:
            if self._camera:
                self._camera.stop()

    def get_jpeg_frame_generator(self):
        """Get a generator that yields JPEG-encoded frames.

        This is optimized for MJPEG streaming over HTTP.
        Each frame is a complete JPEG image that can be
        transmitted directly.

        Yields
        ------
        bytes
            JPEG-encoded frame data

        Raises
        ------
        RuntimeError
            If camera is not initialized or
            streaming fails

        Notes
        -----
        This generator runs indefinitely. Use in a
        loop and break when done. Each frame is a
        valid JPEG image suitable for MJPEG streams.
        """
        import io

        if self._camera is None:
            raise RuntimeError("Camera not initialized")

        try:
            config = self._camera.create_still_configuration(
                main={"size": (self.width, self.height)}
            )
            self._camera.configure(config)
            self._camera.start()

            while True:
                # Capture still image to BytesIO buffer
                buffer = io.BytesIO()
                self._camera.capture_file(buffer, format="jpeg")
                jpeg_data = buffer.getvalue()
                yield jpeg_data

        except Exception as e:
            raise RuntimeError(f"JPEG streaming failed: {e}") from e
        finally:
            if self._camera:
                self._camera.stop()

    def close(self) -> None:
        """Clean up camera resources.

        Stops any active recording and closes the
        camera.
        """
        if self._is_recording:
            try:
                self.stop_recording()
            except RuntimeError:
                pass

        if self._camera is not None:
            self._camera.close()
            self._camera = None

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        self.close()
        return False

    def __repr__(self) -> str:
        """Return string representation of camera."""
        return (
            f"Camera(index={self.camera_index}, "
            f"resolution={self.width}x{self.height}, "
            f"fps={self.fps}, encoder={self.encoder})"
        )
