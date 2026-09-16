"""Web streaming server for Raspberry Pi camera.

This module provides an HTTP server for streaming live camera
feed via MJPEG (Motion JPEG) protocol. The stream can be viewed
in any web browser without external plugins.

Classes
-------
StreamServer
    HTTP server for serving live camera MJPEG stream.
"""

import hmac
import threading
from typing import Optional

try:
    from flask import Flask, Response, render_template_string, request
except ImportError:
    Flask = None  # type: ignore
    Response = None  # type: ignore
    render_template_string = None  # type: ignore
    request = None  # type: ignore

try:
    from werkzeug.serving import BaseWSGIServer
except ImportError:
    BaseWSGIServer = None  # type: ignore

from nomothetic.camera import Camera

# HTML template for the viewer page
VIEWER_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Nomon Camera Stream</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
                         Roboto, 'Helvetica Neue', Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            background-color: #1a1a1a;
            color: #fff;
        }
        .container {
            text-align: center;
            padding: 20px;
            max-width: 1000px;
        }
        h1 {
            margin-top: 0;
            font-size: 28px;
        }
        .stream-wrapper {
            background-color: #000;
            border-radius: 8px;
            padding: 10px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
            margin: 20px 0;
        }
        .stream-wrapper img {
            max-width: 100%;
            height: auto;
            border-radius: 4px;
            display: block;
        }
        .info {
            font-size: 14px;
            color: #999;
            margin-top: 15px;
        }
        .info p {
            margin: 5px 0;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎥 Nomon Camera Stream</h1>
        <div class="stream-wrapper">
            <img src="{{ stream_src }}" alt="Camera Stream">
        </div>
        <div class="info">
            <p>Resolution: {{ width }}x{{ height }}</p>
            <p>Frame Rate: {{ fps }} fps</p>
            <p>Encoder: {{ encoder }}</p>
        </div>
    </div>
</body>
</html>
"""


class StreamServer:
    """HTTP server for Raspberry Pi camera MJPEG streaming.

    Provides a simple web interface to view live camera feed via
    MJPEG (Motion JPEG) protocol over HTTP. Works in any browser
    without plugins or external libraries.

    The server can either create a ``Camera`` instance internally or accept
    an existing one via the ``camera`` parameter.  When an existing camera is
    provided the server does **not** close it on :meth:`close`, leaving
    lifecycle management to the caller.

    Parameters
    ----------
    host : str, optional
        Host to bind to (default: "localhost")
    port : int, optional
        Port to bind to (default: 8000)
    camera_index : int, optional
        Camera index to use when creating a new camera (default: 0).
        Ignored when ``camera`` is provided.
    width : int, optional
        Capture width in pixels (default: 1280).
        Ignored when ``camera`` is provided.
    height : int, optional
        Capture height in pixels (default: 720).
        Ignored when ``camera`` is provided.
    fps : int, optional
        Frames per second (default: 30).
        Ignored when ``camera`` is provided.
    encoder : str, optional
        Video encoder: 'h264' or 'mjpeg' (default: 'h264').
        Ignored when ``camera`` is provided.
    camera : Camera, optional
        Existing ``Camera`` instance to stream from.  When provided the
        server does **not** own the camera and will not close it on
        :meth:`close`.  All camera-related keyword arguments are ignored.
    access_token : str, optional
        When set, every request to ``/`` and ``/stream`` must carry a
        matching ``?token=`` query parameter or it is rejected with 403.
        The MJPEG stream runs over plain HTTP outside the authenticated
        REST API, so this token is what gates camera access (security
        checklist P10).  ``None`` disables the check (library use only).
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8000,
        camera_index: int = 0,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        encoder: str = "h264",
        camera: Optional[Camera] = None,
        access_token: Optional[str] = None,
    ) -> None:
        """Initialize the streaming server.

        Parameters
        ----------
        host : str, optional
            Host to bind to (default: "localhost")
        port : int, optional
            Port to bind to (default: 8000)
        camera_index : int, optional
            Camera index to use when creating a new camera (default: 0).
            Ignored when ``camera`` is provided.
        width : int, optional
            Capture width in pixels (default: 1280).
            Ignored when ``camera`` is provided.
        height : int, optional
            Capture height in pixels (default: 720).
            Ignored when ``camera`` is provided.
        fps : int, optional
            Frames per second (default: 30).
            Ignored when ``camera`` is provided.
        encoder : str, optional
            Video encoder: 'h264' or 'mjpeg' (default: 'h264').
            Ignored when ``camera`` is provided.
        camera : Camera, optional
            Existing ``Camera`` instance to stream from.  When provided the
            server does **not** own the camera and will not close it on
            :meth:`close`.  All camera-related keyword arguments are ignored.
        access_token : str, optional
            Required ``?token=`` query value for ``/`` and ``/stream``.
            ``None`` disables the check.

        Raises
        ------
        RuntimeError
            If Flask is not installed
        ValueError
            If port is not in valid range (1-65535)
        """
        if Flask is None:
            raise RuntimeError(
                "Flask not available. " "Install with: pip install 'nomothetic[web]'"
            )

        if not 1 <= port <= 65535:
            raise ValueError(f"Port must be between 1 and 65535, got {port}")

        self.host = host
        self.port = port
        self._access_token = access_token

        if camera is not None:
            # Use the provided camera; caller retains ownership.
            self.camera = camera
            self._owns_camera = False
            self.width = camera.width
            self.height = camera.height
            self.fps = camera.fps
            self.encoder = camera.encoder
        else:
            self.width = width
            self.height = height
            self.fps = fps
            self.encoder = encoder
            self._owns_camera = True
            # Create camera instance
            self.camera = Camera(
                camera_index=camera_index,
                width=width,
                height=height,
                fps=fps,
                encoder=encoder,
            )

        # Thread synchronization for frame sharing
        self._frame_lock = threading.Lock()
        self._current_frame: Optional[bytes] = None

        # Stop event — set to request server and streaming generators to halt
        self._stop_event = threading.Event()

        # Werkzeug BaseWSGIServer handle — populated by start()
        self._httpd: Optional[BaseWSGIServer] = None

        # Background thread handle — populated by start_background()
        self._thread: Optional[threading.Thread] = None

        # Create Flask app
        self.app = Flask(__name__)
        self.app.add_url_rule("/", "viewer", self._viewer)
        self.app.add_url_rule("/stream", "stream", self._stream_endpoint)

    def _supplied_token(self) -> str:
        """Return the ``?token=`` value from the current request (may be empty).

        Isolated in its own method so tests can stub it without touching
        Flask's ``request`` proxy (which cannot be introspected outside a
        request context).
        """
        supplied = request.args.get("token", "")
        return supplied if isinstance(supplied, str) else ""

    def _token_ok(self) -> bool:
        """Return True when no token is configured or the request carries it."""
        if self._access_token is None:
            return True
        return hmac.compare_digest(self._supplied_token(), self._access_token)

    def _viewer(self):
        """Serve the HTML viewer page (403 without a valid access token).

        Returns
        -------
        str or tuple
            Rendered HTML template with camera parameters, or a
            ``(body, 403)`` tuple when the access token is missing/wrong.
        """
        if not self._token_ok():
            return "Forbidden: missing or invalid stream token", 403
        stream_src = "/stream"
        if self._access_token is not None:
            stream_src = f"/stream?token={self._access_token}"
        return render_template_string(
            VIEWER_TEMPLATE,
            width=self.width,
            height=self.height,
            fps=self.fps,
            encoder=self.encoder,
            stream_src=stream_src,
        )

    def _stream_endpoint(self) -> Response:
        """Stream MJPEG frames to the client (403 without a valid access token).

        Returns
        -------
        Response
            Flask response with multipart/x-mixed-replace content type
            that streams JPEG frames continuously
        """
        if not self._token_ok():
            return Response("Forbidden: missing or invalid stream token", status=403)

        def generate():
            """Generator that yields MJPEG boundary data."""
            try:
                for jpeg_frame in self.camera.get_jpeg_frame_generator():
                    if self._stop_event.is_set():
                        break
                    # Wrap each frame in MJPEG boundary
                    boundary = b"--frame"
                    content_type = b"Content-Type: image/jpeg"
                    content_length = b"Content-Length: " + str(len(jpeg_frame)).encode()
                    crlf = b"\r\n"

                    yield boundary + crlf
                    yield content_type + crlf
                    yield content_length + crlf
                    yield crlf
                    yield jpeg_frame
                    yield crlf
            except GeneratorExit:
                # Client disconnected, clean up
                pass
            except Exception as e:
                # Log error but continue (client may have disconnected)
                print(f"Stream error: {e}")

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    def start(self, debug: bool = False) -> None:
        """Start the streaming server (blocking).

        Parameters
        ----------
        debug : bool, optional
            Enable Flask debug mode (default: False).
            Note: Not recommended for production.

        Notes
        -----
        This method blocks until the server is stopped.
        Navigate to http://localhost:8000 (or configured host:port)
        to view the stream.
        """
        try:
            from werkzeug.serving import make_server

            self._httpd = make_server(self.host, self.port, self.app)

            if self._httpd:
                self._httpd.serve_forever()
        except KeyboardInterrupt:
            # Handle Ctrl+C gracefully
            pass
        finally:
            self._httpd = None
            self.close()

    def start_background(self) -> threading.Thread:
        """Start the streaming server in a background thread.

        Returns
        -------
        threading.Thread
            The thread running the server. Call join() to wait
            for it to complete.

        Notes
        -----
        This is useful for testing or running the server
        alongside other code. The server will continue running
        until close() is called or the main program exits.
        """
        thread = threading.Thread(
            target=self.start,
            kwargs={"debug": False},
            daemon=True,
        )
        thread.start()
        self._thread = thread
        return thread

    def close(self) -> None:
        """Shut down the server and clean up resources.

        Signals the MJPEG frame generators to stop, shuts down the Werkzeug
        HTTP server (so the port is released and the background thread exits),
        joins the background thread, and closes the camera only if this server
        created it (i.e. no external ``camera`` was passed to :meth:`__init__`).
        """
        self._stop_event.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None
        thread = getattr(self, "_thread", None)
        # start()'s finally also calls close() from the server thread itself
        # once serve_forever() returns — a thread cannot join itself.
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        if self._owns_camera:
            self.camera.close()

    def __repr__(self) -> str:
        """Return string representation of server."""
        return (
            f"StreamServer(host={self.host}, port={self.port}, "
            f"resolution={self.width}x{self.height}, "
            f"fps={self.fps}, encoder={self.encoder})"
        )
