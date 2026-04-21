import os
import json
import shutil
import datetime
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(".env")
openai_key = os.getenv('OPENAI_API_KEY', '').strip()
client = OpenAI(api_key=openai_key)

# Directories
RAW_DIR = Path("transcripts/raw")
ARCHIVE_DIR = Path("transcripts/archive")
MEMORY_BANK = Path("memory_bank")
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_BANK.mkdir(exist_ok=True)

def load_schema():
    with open("BRAIN_SCHEMA.md", "r") as f:
        schema = f.read()
    with open("LIFE_BUCKETS.md", "r") as f:
        buckets = f.read()
    return schema, buckets

def process_transcript(file_path: Path):
    print(f"\n🧠 Archivist waking up. Processing: {file_path.name}")
    with open(file_path, "r") as f:
        content = f.read()
    
    schema, buckets = load_schema()
    
    # We use a highly rigid prompt forcing JSON output so Python can parse it perfectly.
    prompt = f"""
    You are the Archivist AI for Jay's Personal Memory Bank.
    
    # OBJECTIVE:
    Extract the key autobiographical data from the raw transcript.
    Map the data entirely into ATOMIC NODES within the MEMORY BANK.
    
    # RULES:
    1. Read the SCHEMA to understand the folder structures.
    2. Read the BUCKETS to understand what human data is important.
    3. Generate new atomic markdown entries with YAML metadata tags.
    4. Link concepts using [[WikiLinks]].
    
    # OUTPUT FORMAT:
    Output ONLY a valid JSON object. No explanation.
    {{
        "modifications": [
            {{
                "folder": "timeline",
                "filename": "FreightIQ_Founding.md",
                "content_to_append": "---\\ntags: [freightiq, startups]\\ndate: 2026-04-18\\n---\\n\\n## New Entry\\nJay realized that..."
            }}
        ]
    }}
    
    # TRANSCRIPT TO PROCESS:
    {content}
    
    # INSTRUCTIONS:
    - {schema}
    - {buckets}
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": prompt}]
        )
        data = json.loads(response.choices[0].message.content)
        
        for mod in data.get('modifications', []):
            folder_path = MEMORY_BANK / mod['folder']
            folder_path.mkdir(parents=True, exist_ok=True)
            target_file = folder_path / mod['filename']
            
            # Simple append/create logic
            is_new = not target_file.exists() or target_file.stat().st_size == 0
            with open(target_file, "a") as f:
                if is_new:
                    # Initialize atomic node with Title 
                    clean_title = mod['filename'].replace('.md', '').replace('_', ' ')
                    f.write(f"# {clean_title}\n\n")
                f.write(mod['content_to_append'] + "\n\n")
            print(f"  ✅ Updated Atomic Node: {mod['folder']}/{mod['filename']}")
            
        # Move the raw file to COLD Storage (archive) so we don't process it twice
        shutil.move(str(file_path), str(ARCHIVE_DIR / file_path.name))
        print(f"  📦 Safely Archived raw transcript to {ARCHIVE_DIR.name}")
            
    except Exception as e:
        print(f"  ❌ Error processing {file_path.name}: {e}")

def sync_to_github():
    """Autonomously pushes the new Memory Bank files back to the remote repository."""
    print("\n🔄 Initiating Cloud-to-Local Git Sync...")
    try:
        # Railway containers lack a default Git identity
        subprocess.run(["git", "config", "--global", "user.email", "archivist@railway.app"])
        subprocess.run(["git", "config", "--global", "user.name", "AI Archivist Bot"])

        # Add all new Markdown changes
        subprocess.run(["git", "add", "."], check=True)
        
        # Commit
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        subprocess.run(["git", "commit", "-m", f"Archivist: Auto-sync Brain {timestamp}"], check=True)
        
        # Inject Railway cloud credentials if available
        pat = os.getenv('GITHUB_PAT')
        if pat:
            subprocess.run(["git", "remote", "set-url", "origin", f"https://jayzhuang1996:{pat}@github.com/jayzhuang1996/Personal-Memory.git"], check=True)
        
        # Push to remote 
        subprocess.run(["git", "push", "origin", "main"], check=True)
        print("✅ Successfully pushed new Memory Bank files to GitHub.")
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Git Sync encountered an issue (perhaps no changes to commit?): {e}")

def main():
    if not os.path.exists(RAW_DIR):
        print("No raw transcripts found to process.")
        return
        
    files = list(RAW_DIR.glob("*.md"))
    if not files:
        print("No new transcripts to process. Brain is fully synced.")
        return
        
    print(f"Found {len(files)} new transcript(s) in inbox.")
    for file in files:
        process_transcript(file)
        
    # Trigger the Git Push so the cloud files go back to the user's IDE
    sync_to_github()

if __name__ == '__main__':
    main()
