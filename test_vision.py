import os
from dotenv import load_dotenv
from google import genai

# Load API key
load_dotenv()

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Say hello to Tejal and tell me Gemini Vision is working!"
)

print(response.text)