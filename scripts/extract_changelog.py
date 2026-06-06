import os
import sys

def main():
    tag = os.environ.get("GITHUB_REF_NAME", "")
    version = tag.lstrip('v') if tag else ""
    
    changelog_path = "CHANGELOG.md"
    if not os.path.exists(changelog_path):
        print(f"Error: {changelog_path} not found.", file=sys.stderr)
        sys.exit(1)
        
    try:
        with open(changelog_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Error reading {changelog_path}: {e}", file=sys.stderr)
        sys.exit(1)
        
    if not version:
        for line in lines:
            if line.startswith("## "):
                parts = line.split()
                if len(parts) > 1:
                    version = parts[1].strip("[]")
                    break
        print(f"No tag version provided, auto-detected latest version: {version}")
        
    release_content = []
    started = False
    
    for line in lines:
        if line.startswith("## "):
            if started:
                break
            if version in line:
                started = True
                continue
        if started:
            release_content.append(line)
            
    changelog_text = "".join(release_content).strip()
    if not changelog_text:
        print(f"Warning: Could not find changelog section for version '{version}' in {changelog_path}.", file=sys.stderr)
        changelog_text = f"Release {tag or 'latest'}"
        
    output_path = "release_notes.md"
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(changelog_text)
        print(f"Successfully wrote release notes for version '{version}' to {output_path}")
    except Exception as e:
        print(f"Error writing output file: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
