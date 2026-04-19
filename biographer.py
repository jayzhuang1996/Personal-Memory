import os
import datetime
import random

# A seed list of deep, autobiographical questions
QUESTIONS = [
    "What was the most defining failure you experienced in your early 20s, and how did it shape your current engineering philosophy?",
    "Think back to a moment in your childhood that fundamentally shaped your view on hard work. Describe it.",
    "Which project or codebase are you most proud of building, and what was the darkest moment during its development?",
    "Describe your core philosophy when it comes to adopting AI and new technologies. Has it changed over the last two years?",
    "Who was the most influential mentor in your life, and what is one rule they taught you that you still follow?",
    "If you had to summarize your identity outside of being an engineer in three sentences, what would you say?"
]

def run_interview():
    print("\n" + "="*50)
    print("🧠 THE BIOGRAPHER AI - DAILY INTERVIEW")
    print("="*50)
    
    # Select a random question for the day
    q = random.choice(QUESTIONS)
    print(f"\nToday's Prompt:\n> {q}\n")
    
    print("(You can record on your phone and paste the transcription here, or just type freely. Press ENTER twice when done.)")
    print("\nYour Answer:")
    
    # Read multi-line input
    lines = []
    while True:
        line = input()
        if not line:
            break
        lines.append(line)
        
    response = "\n".join(lines).strip()
    
    if not response:
        print("No input provided. Interview canceled.")
        return

    # Save to transcripts folder
    os.makedirs("transcripts", exist_ok=True)
    today = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    filename = f"transcripts/interview_{today}.md"
    
    with open(filename, "w") as f:
        f.write(f"# Interview {today}\n\n")
        f.write(f"**Prompt:** {q}\n\n")
        f.write(f"**Response:**\n{response}\n")
        
    print(f"\n✅ Transcript saved to {filename}")
    print("Next Step: In your IDE chat, simply tell your Agent: 'Process my latest transcript' to compile it into your Brain.")

if __name__ == "__main__":
    run_interview()
