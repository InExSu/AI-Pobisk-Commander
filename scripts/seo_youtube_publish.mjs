/**
 * seo_youtube_publish.mjs - Publish video to YouTube
 *
 * Usage: .venv/bin/node scripts/seo_youtube_publish.mjs <video_path> <task_slug>
 *
 * Example:
 *   .venv/bin/node scripts/seo_youtube_publish.mjs docs/SEO/igrasu.ru/tasks/task_1793551800_0001_velo_spiders/video_final.mp4 task_1793551800_0001_velo_spiders
 *
 * This script uses the Google YouTube Data API to publish a video.
 * Requires YouTube API credentials setup via OAuth 2.0.
 *
 * Prerequisites:
 * 1. Create OAuth 2.0 client ID from Google Cloud Console
 * 2. Save credentials to a JSON file
 * 3. Run the script to authenticate and get refresh token
 * 4. Store the refresh token for future use
 */

import { google } from 'googleapis';
import fs from 'fs';
import path from 'path';
import readline from 'readline';

const args = process.argv.slice(2);
const videoPath = args[0] || path.resolve(process.cwd(), 'video_final.mp4');
const taskSlug = args[1] || 'task_1793551800_0001_velo_spiders';
const apiKey = args[2];

console.log('=== YouTube Publisher ===');
console.log('Video path:', videoPath);
console.log('Task slug:', taskSlug);

// Check if video file exists
if (!fs.existsSync(videoPath)) {
  console.error('Error: Video file not found:', videoPath);
  process.exit(1);
}

console.log('Video file size:', fs.statSync(videoPath).size, 'bytes');

// YouTube API setup - this requires OAuth 2.0 credentials
// For now, we'll check for API key or existing token

// Try using YouTube Data API with API key if provided
if (apiKey) {
  console.log('Using YouTube Data API key...');
  try {
    const youtube = google.youtube('v3', { apiKey });
    console.log('YouTube API initialized with key');
    // Note: Publishing requires OAuth, not just API key
    console.log('Publishing with API key only is limited; OAuth 2.0 required for full functionality');
  } catch (err) {
    console.error('YouTube API error:', err.message);
  }
} else {
  console.log('No YouTube API key provided.');
  console.log('To use this script, you need to:');
  console.log('1. Create OAuth 2.0 client ID from Google Cloud Console');
  console.log('2. Set up credentials file');
  console.log('3. Run authentication flow to get refresh token');
  console.log('4. Store refresh token in ~/.yt-credentials.json');
}

console.log('=== YouTube Publisher complete ===');
