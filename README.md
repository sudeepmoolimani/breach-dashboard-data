# Breach Dashboard Data Host

Free setup using GitHub Actions + GitHub Pages.

## What this does

- Reads public Google Drive folder:
  `1yg06zp_7OMYZzkNnHDovUSHF7zdW8mTc`
- Reads public Conversion Google Drive folder:
  `1F2YPnY2PbFaR8MY-mXn8pqM_B6xd9Ejt`
- Parses `.xlsb` / `.xlsx` files using the same dashboard rules.
- Generates `public/breach-data.js` and `public/conversion-data.js`.
- GitHub Pages hosts that file for every laptop.

## Setup

1. Create a free GitHub account.
2. Create a new public repository, for example:
   `breach-dashboard-data`
3. Upload all files from this folder to that repository.
4. In GitHub repo, go to **Settings > Pages**.
5. Source: choose **GitHub Actions**.
6. Go to **Actions** tab.
7. Run **Generate Breach Dashboard Data** manually whenever you upload new files.
8. After it finishes, your data URL will be:

```text
https://YOUR-GITHUB-USERNAME.github.io/breach-dashboard-data/breach-data.js
```

Then update dashboard HTML:

```html
window.BREACH_REMOTE_DATA_JS = 'https://YOUR-GITHUB-USERNAME.github.io/breach-dashboard-data/breach-data.js'
window.CONVERSION_REMOTE_DATA_JS = 'https://YOUR-GITHUB-USERNAME.github.io/breach-dashboard-data/conversion-data.js'
```

GitHub will refresh only when you open **Actions > Generate Breach Dashboard Data > Run workflow**.

Note: Conversion files are very large, so the workflow can take time to finish.
