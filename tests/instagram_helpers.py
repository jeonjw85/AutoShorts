import uuid

from shorts_local.instagram_models import InstagramPost, ScanBatch, SourceMedia


def post(code="Ab", published=200, **changes):
    return InstagramPost(**(dict(shortcode=code, url=f"https://instagram.com/p/{code}/",
                                 author="pets", owner_id="123", published_at=published,
                                 caption="원본 영상", media_type="video") | changes))


def batch(source, posts=(), **changes):
    return ScanBatch(**(dict(run_id=uuid.uuid4().hex, source_id=source["id"],
                            base_revision=source["scan_revision"], started_at=300,
                            account_id="123", username="pets", posts=list(posts),
                            complete=True) | changes))


def media(code="Ab", **changes):
    return SourceMedia(**(dict(post=post(code), sha256="a" * 64, bytes=123,
                              duration=10, width=640, height=360, has_audio=True) | changes))


def make_source(folder, *, seconds=2, audio=True, silent=False, width=320, height=180):
    import hashlib
    import subprocess

    from shorts_local.media import probe

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "source.mp4"
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            f"testsrc2=size={width}x{height}:rate=30"]
    if audio:
        signal = "anullsrc=r=16000:cl=mono" if silent else "sine=frequency=440:sample_rate=16000"
        args += ["-f", "lavfi", "-i", signal]
    subprocess.run([*args, "-t", str(seconds), "-c:v", "libx264", "-threads", "1",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True, capture_output=True)
    info = probe(path)
    return SourceMedia(post=post(), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                       bytes=path.stat().st_size, duration=float(info["format"]["duration"]),
                       width=width, height=height, has_audio=audio)
