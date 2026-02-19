
import uvicorn
import os
import sys

# Ensure we are in the backend directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    print("🚀 Starting Video Voice Translator Backend...")
    print("URL: http://localhost:9100")
    
    # Run uvicorn programmatically
    uvicorn.run(
        "app.main:app", 
        host="0.0.0.0", 
        port=9100, 
        reload=True
    )
