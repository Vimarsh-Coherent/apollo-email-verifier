# Apollo Scraper - JSON to CSV Converter

A Streamlit web application that converts Apollo API JSON responses (both `people` and `contacts` formats) into a standard CSV format.

## Features

- 📊 Convert JSON data to CSV format
- 👥 Extract person and organization information from multiple pages
- 📧 Track email verification status
- 🌍 Geographic data extraction
- 📥 Download results as CSV file
- 📈 Display conversion statistics
- 🔄 Supports both `people` and `contacts` JSON structures
- 📄 Process up to 25 pages of data simultaneously

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

## Local Usage

1. Run the Streamlit app:
```bash
streamlit run app.py
```

2. Open your browser to the URL shown in the terminal (usually `http://localhost:8501`)

3. Paste JSON data from each Apollo page into the corresponding tab

4. Click "Convert All Pages to CSV"

5. Download the generated CSV file

## Streamlit Cloud Deployment

### Prerequisites
- A GitHub account
- Your code pushed to a GitHub repository

### Deployment Steps

1. **Push your code to GitHub:**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin <your-github-repo-url>
   git push -u origin main
   ```

2. **Deploy on Streamlit Cloud:**
   - Go to [share.streamlit.io](https://share.streamlit.io)
   - Sign in with your GitHub account
   - Click "New app"
   - Select your repository
   - Set the main file path to: `app.py`
   - Click "Deploy"

3. **Your app will be live at:**
   `https://<your-app-name>.streamlit.app`

### Streamlit Cloud Configuration

The app is ready for Streamlit Cloud with:
- ✅ `requirements.txt` with all dependencies
- ✅ No local file dependencies
- ✅ All data processing in memory
- ✅ No environment variables required

## CSV Output Columns

The CSV includes the following columns:

### Person Information
- id, name, first_name, last_name
- title, headline
- email, email_status
- linkedin_url
- city, state, country, time_zone
- seniority

### Organization Information
- organization_id, organization_name
- org_name, org_website, org_linkedin
- org_employees, org_industries
- org_keywords, org_phone, org_founded_year

## Requirements

- Python 3.7+
- streamlit
- pandas
