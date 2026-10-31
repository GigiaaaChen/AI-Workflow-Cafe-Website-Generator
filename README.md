# Café Site

Create an editable café or restaurant website using a public menu URL. It is a local Python/FastAPI application. The generated site includes an AI-powered review reviser followed by a product menu and an optional manually uploaded environment-photo slideshow. It does not take orders or payments.

## Install and run

1. Extract this ZIP and open the **cafe-site** folder in a terminal.
2. Install the Python packages and Playwright browser:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

3. Make your configuration file and edit it with your OpenAI key:

In `.env`, set `OPENAI_API_KEY=your_key_here`. Keep this key private.

4. Run the server:

```powershell
.venv\Scripts\python -m uvicorn app:app --port 3000
```

5. In a browser visit **http://localhost:3000**. Keep the terminal open while using the application.

## How to use

1. Paste the restaurant's publicly accessible official menu website URL and start importing.
2. Watch the progress and review the import report. Imports are best-effort and may miss items, sections, images, or descriptions.
3. Edit the business name, main categories, subcategories, product names, and descriptions. Use **Add Item** to insert a product and **Remove item** to delete it.
4. Add or replace each menu product's image using its file chooser. Upload environment photos separately if desired; they are not auto-extracted.
5. Select a visual style/accent color, generate the website, and inspect its preview.
6. Download the generated HTML once you are satisfied.

## Generated website order

1. Business introduction
2. **AI review reviser** (Natural, Friendly, Short, or Polished; requires running backend)
3. Menu grouped by categories and subcategories
4. About section and manually uploaded environment-photo gallery

The review reviser requires a deployed backend with an API key. It will not work when the downloaded HTML is opened directly as a `file://` URL.


## Limitations

Menu import is best-effort; some sites need store selection or expose content only through interactive interfaces. The crawler has finite limits. Product photos may require permission before reuse. A valid API key is required to generate the site's marketing text and to enable review rewriting. No live import against Pret, Starbucks, or HEYTEA has been verified for this revision.

## About the café field

The optional About the café field shows a suggestion until you begin typing. Enter your own café details for AI to expand them into website copy, or leave the field blank to generate a description from the business name and imported menu. The suggestion is placeholder text, not submitted as café information.
