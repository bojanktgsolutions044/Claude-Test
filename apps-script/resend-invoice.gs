/**
 * Resend-Invoice automation  (Google Apps Script)
 * ------------------------------------------------------------------
 * Scans the Gmail inbox for client replies asking us to (re)send or
 * attach their invoice, looks the client up in the invoice sheet by
 * email, and creates a Gmail DRAFT reply with the correct invoice PDF
 * ATTACHED.
 *
 * It NEVER sends mail - it only creates drafts for your review.
 *
 * Why this lives here (not in the Claude /email-loop routine):
 *   Claude's Gmail connector cannot attach files to drafts. Apps Script
 *   can, so the "attach the same invoice" requirement is handled here.
 *
 * One-time setup:
 *   1. Open the sheet -> Extensions -> Apps Script.
 *   2. Paste this file. Save.
 *   3. Run `setup()` once and grant the permissions it requests.
 *      (Installs an hourly trigger and sets Overview!B2.)
 *
 * Adjust the CONFIG block if tab names / columns / month change.
 */

const CONFIG = {
  spreadsheetId: '1BeHoRp59FFuGAG4jxYMdVlmJSz5L9SkD-QkzGUutPOY',
  dataTabGid: 660451316,        // invoice-rows tab (resolved by gid, not by name)
  overviewTabName: 'Overview',
  month: 'May 2026',            // the invoice month being handled

  // 1-based column numbers in the data tab:
  colMonth: 2,                  // B  Invoice for Month
  colInvoiceLink: 7,            // G  Invoice Link for Accounting (Drive URL)
  colEmail: 11,                 // K  Email For Invoicing

  gmailSearch: 'in:inbox is:unread newer_than:7d',

  // Phrases signalling the client wants the invoice (re)sent/attached:
  triggerPhrases: [
    'resend', 're-send', 'send it again', 'send again',
    'attach the invoice', 'attach invoice', 'send the invoice',
    'send me the invoice', 'lost the invoice', "can't find the invoice",
    'cannot find the invoice', "didn't receive", 'did not receive',
    'where is the invoice', 'copy of the invoice', 'the invoice again',
    'resend it', 'resend the invoice'
  ],

  draftLabel: 'auto-invoice-draft' // applied to handled threads to avoid duplicates
};

/** Run once. Installs the hourly trigger, creates the dedupe label, sets B2. */
function setup() {
  if (!GmailApp.getUserLabelByName(CONFIG.draftLabel)) {
    GmailApp.createLabel(CONFIG.draftLabel);
  }
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'processInvoiceRequests')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('processInvoiceRequests').timeBased().everyHours(1).create();
  ensureOverviewMonth();
  Logger.log('Setup complete. Hourly trigger installed.');
}

/** Write the month into Overview!B2 if it is not already set. */
function ensureOverviewMonth() {
  const ss = SpreadsheetApp.openById(CONFIG.spreadsheetId);
  const ov = ss.getSheetByName(CONFIG.overviewTabName);
  if (!ov) return;
  const b2 = ov.getRange('B2');
  if (String(b2.getValue()).trim() !== CONFIG.month) {
    b2.setValue(CONFIG.month);
  }
}

/** The data tab, resolved by gid so a tab rename will not break it. */
function getDataSheet_() {
  const ss = SpreadsheetApp.openById(CONFIG.spreadsheetId);
  const sheet = ss.getSheets().find(s => s.getSheetId() === CONFIG.dataTabGid);
  if (!sheet) throw new Error('Data tab gid ' + CONFIG.dataTabGid + ' not found');
  return sheet;
}

/** Map of {emailLower -> Drive file id} for the configured month. */
function buildInvoiceIndex_() {
  const values = getDataSheet_().getDataRange().getValues();
  const index = {};
  for (let r = 1; r < values.length; r++) { // skip header
    if (String(values[r][CONFIG.colMonth - 1]).trim() !== CONFIG.month) continue;
    const fileId = extractDriveId_(String(values[r][CONFIG.colInvoiceLink - 1]).trim());
    if (!fileId) continue;
    String(values[r][CONFIG.colEmail - 1]).toLowerCase()
      .split(/[,;\s]+/).filter(Boolean)
      .forEach(e => { index[e] = fileId; });
  }
  return index;
}

function extractDriveId_(url) {
  const m = url.match(/\/d\/([a-zA-Z0-9_-]+)/) || url.match(/[?&]id=([a-zA-Z0-9_-]+)/);
  return m ? m[1] : null;
}

/** Main entry point (also the trigger handler). */
function processInvoiceRequests() {
  ensureOverviewMonth();
  const index = buildInvoiceIndex_();
  const label = GmailApp.getUserLabelByName(CONFIG.draftLabel) || GmailApp.createLabel(CONFIG.draftLabel);

  GmailApp.search(CONFIG.gmailSearch + ' -label:' + CONFIG.draftLabel).forEach(thread => {
    const msgs = thread.getMessages();
    const last = msgs[msgs.length - 1];
    const subject = (last.getSubject() || '');
    const body = (last.getPlainBody() || '').toLowerCase();

    if (subject.toLowerCase().indexOf(CONFIG.month.toLowerCase()) === -1) return;
    if (!CONFIG.triggerPhrases.some(p => body.indexOf(p) !== -1)) return;

    const sender = extractEmail_(last.getFrom()).toLowerCase();
    const fileId = index[sender];
    if (!fileId) return; // sender not found in column K for this month

    const blob = DriveApp.getFileById(fileId).getBlob();
    const draftBody = 'Hello,\n\n'
      + 'Apologies for any trouble locating it. Please find your invoice attached.\n\n'
      + 'Best regards,';

    last.createDraftReply(draftBody, { attachments: [blob] });
    thread.addLabel(label); // mark handled to avoid duplicate drafts next run
  });
}

function extractEmail_(from) {
  const m = from.match(/<([^>]+)>/);
  return m ? m[1] : from.trim();
}
