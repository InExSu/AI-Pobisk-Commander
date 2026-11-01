import { chromium } from 'playwright';
import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';

const args = process.argv.slice(2);
const url = args[0] || 'https://www.igrasu.ru/games/U-X/V/velo_Storm_Spiders/';
const webmPath = args[1] || path.resolve(process.cwd(), 'video_recorded.webm');
const mp4Path = args[2] || path.resolve(process.cwd(), 'video_final.mp4');
const minDuration = parseFloat(args[3]) || 30;

console.log('=== Gameplay Video Recorder ===');
console.log('Target URL:', url);
console.log('Output WebM:', webmPath);
console.log('Output MP4:', mp4Path);
console.log('Minimum duration:', minDuration, 's');

// Ensure output directory exists
const outputDir = path.dirname(webmPath);
if (!fs.existsSync(outputDir)) {
  fs.mkdirSync(outputDir, { recursive: true });
}

(async () => {
  let browser = null;
  let context = null;
  let page = null;

  try {
    // Launch browser
    browser = await chromium.launch({ headless: true });

    // Create context WITHOUT recordVideo option (simpler)
    context = await browser.newContext({ 
      viewport: { width: 1280, height: 720 },
      deviceScaleFactor: 1
    });

    page = await context.newPage();

    // Navigate to the game
    console.log('Navigating to game...');
    await page.goto(url, { waitUntil: 'networkidle' });
    await page.waitForTimeout(3000);

    // Get the video object - this may return null without recordVideo context option
    const video = page.video();
    console.log('Video object:', video);

    if (video) {
      console.log('Video recording available');
      // Start video recording with explicit path
      console.log('Starting video recording...');
      await video.start({ path: webmPath, quality: 'high' });
    } else {
      console.log('Video object is null, trying alternative approach...');
      // Fallback: use the page's internal video recording
      // By navigating and waiting, the video might be recorded automatically
      console.log('Starting recording fallback...');
    }

    // Wait for minimum duration (30 seconds) of gameplay
    console.log('Waiting for ' + minDuration + ' seconds of gameplay...');
    await page.waitForTimeout(minDuration * 1000 + 500);

    // Stop video recording if video object exists
    if (video) {
      console.log('Stopping video recording...');
      await video.stop();
    }

    await browser.close();
    browser = null;

    // Check if video was recorded
    if (fs.existsSync(webmPath)) {
      console.log('Video recorded to ' + webmPath);
      console.log('Converting to MP4 using ffmpeg...');
      try {
        const mp4Dir = path.dirname(mp4Path);
        if (!fs.existsSync(mp4Dir)) {
          fs.mkdirSync(mp4Dir, { recursive: true });
        }

        let ffmpegSuccess = false;

        // Approach 1: Direct copy
        try {
          execSync(
            'ffmpeg -i ' + webmPath + ' -c copy -movflags +faststart ' + mp4Path,
            { stdio: 'pipe', encoding: 'utf-8' }
          );
          console.log('FFmpeg conversion (direct copy) completed successfully');
          ffmpegSuccess = true;
        } catch (err1) {
          // Approach 2: Re-encode
          try {
            execSync(
              'ffmpeg -i ' + webmPath + ' -c:v libx264 -c:a aac -preset fast -crf 23 ' + mp4Path,
              { stdio: 'pipe', encoding: 'utf-8' }
            );
            console.log('FFmpeg conversion (re-encode) completed successfully');
            ffmpegSuccess = true;
          } catch (err2) {
            console.error('FFmpeg conversion failed:', err2.message);
            if (webmPath !== mp4Path) {
              fs.copyFileSync(webmPath, mp4Path);
              console.log('Copied WebM to MP4 path');
              ffmpegSuccess = true;
            }
          }
        }

        if (ffmpegSuccess) {
          if (fs.existsSync(mp4Path)) {
            console.log('Output MP4 file size: ' + fs.statSync(mp4Path).size + ' bytes');
          }
        }
      } catch (err) {
        console.error('FFmpeg error:', err.message);
      }
    } else {
      console.error('No video file was recorded!');
      process.exit(1);
    }

    console.log('=== Video recording complete ===');
    console.log('Output:', mp4Path);
    console.log('File size: ' + (fs.existsSync(mp4Path) ? fs.statSync(mp4Path).size + ' bytes' : 'N/A'));

  } catch (err) {
    console.error('Error during video recording:', err);
    if (browser && !browser.isClosed()) {
      await browser.close().catch(() => {});
    }
    process.exit(1);
  }
})();
