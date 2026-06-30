from google import genai
from PIL import Image
from dotenv import load_dotenv
import os

# Load API key
load_dotenv()

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

# Open image
img = Image.open("shirt.jpg")

# Ask Gemini
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=[
        "Describe this outfit in detail and suggest matching colors.",
        img
    ]
)

print(response.text)