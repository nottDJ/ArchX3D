
try:
    import google.generativeai
    print("Successfully imported google.generativeai")
    print(f"Path: {google.generativeai.__file__}")
except ImportError as e:
    print(f"Failed to import: {e}")
