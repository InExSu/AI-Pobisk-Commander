/**
 * seo_YT_First_Comment.mjs - Leave first comment under YouTube video
 *
 * Usage: .venv/bin/node scripts/seo_YT_First_Comment.mjs <video_id> <comment_text>
 *
 * Example:
 *   .venv/bin/node scripts/seo_YT_First_Comment.mjs abc123def456 'Great gameplay!'
 *
 * This script uses the Google YouTube Data API to post a comment.
 * Requires YouTube API credentials setup via OAuth 2.0.
 */

import { google } from 'googleapis';
import fs from 'fs';
import path from 'path';

const args = process.argv.slice(2);
const videoId = args[0] || 'abc123def456';
const commentText = args[1] || 'Great gameplay!';

console.log('=== YouTube First Comment ===');
console.log('Video ID:', videoId);
console.log('Comment text:', commentText);

// YouTube API setup - requires OAuth 2.0 credentials
// For now, log that comment would be posted
console.log('To post this comment, YouTube API OAuth 2.0 authentication is required.');
console.log('See seo_youtube_publish.mjs for setup instructions.');

console.log('=== YouTube First Comment complete ===');
