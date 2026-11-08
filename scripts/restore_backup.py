"""Validate a backup and optionally restore it into a new isolated directory; dry-run by default."""
import argparse, hashlib, json, shutil, sqlite3, tarfile, tempfile
from contextlib import closing
from pathlib import Path

def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""): h.update(block)
    return h.hexdigest()

def validate(archive, root):
    with tarfile.open(archive,"r:gz") as tar:
        members=tar.getmembers()
        for m in members:
            p=Path(m.name)
            if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
                raise ValueError("Unsafe archive member")
        if not {"manifest.json","donations.db"} <= {m.name for m in members}:
            raise ValueError("Archive incomplete")
        for member in members:
            destination = root / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError("Archive member unreadable")
                with source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
    manifest=json.loads((root/"manifest.json").read_text())
    for name, expected in manifest["sha256"].items():
        p=root/name
        if not p.is_file() or digest(p)!=expected: raise ValueError(f"Checksum mismatch: {name}")
    with closing(sqlite3.connect(root/"donations.db")) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise ValueError("SQLite integrity check failed")
    return manifest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive",type=Path)
    parser.add_argument("--target",type=Path,required=True)
    parser.add_argument("--apply",action="store_true",help="Restore after validation; otherwise dry-run")
    args=parser.parse_args()
    archive=args.archive.resolve(); target=args.target.resolve()
    if not archive.is_file(): raise FileNotFoundError(archive)
    if target.exists(): raise FileExistsError(f"Refusing to overwrite: {target}")
    with tempfile.TemporaryDirectory(prefix="donation-restore-") as tmp:
        root=Path(tmp); manifest=validate(archive,root)
        print(f"Archive valid; SQLite integrity=ok; files={len(manifest['sha256'])}")
        if not args.apply:
            print("Dry-run only; no files restored."); return
        target.mkdir(parents=True,mode=0o700)
        shutil.copy2(root/"donations.db",target/"donations.db")
        if (root/"config.json").is_file(): shutil.copy2(root/"config.json",target/"config.json")
        if (root/"media").is_dir(): shutil.copytree(root/"media",target/"media")
        for p in target.rglob("*"):
            if p.is_file(): p.chmod(0o600)
    print(f"Restored into isolated directory: {target}; live service was not changed.")

if __name__=="__main__": main()
