Place index.html, login.html, and signup.html inside a folder called frontend and keep backend.py beside it.
Create a virtual environment, activate it, and run:
`pip install flask openai python-dotenv werkzeug PyPDF2 python-docx pytesseract pillow pdf2image`
Add a plain-text file called .env (same level as backend.py) that contains just one line:
`OPENAI_API_KEY=sk-your-key-here`
Install Tesseract OCR and Poppler, then be sure their executables are in your system PATH.
Finally run `python backend.py`, open the printed URL in your browser, sign up, and start uploading documents to see summaries, quizzes, chat, languages, prescription-help, and more!
