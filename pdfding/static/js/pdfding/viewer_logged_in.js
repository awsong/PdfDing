// function for getting the signature
async function get_remote_signatures(signature_url) {
  try {
    const response = await fetch(signature_url);
    if (!response.ok) {
      throw new Error(`Response status: ${response.status}`);
    }

    const result = await response.json();
    localStorage.setItem("previous_pdfjs.signature", JSON.stringify(result));
    localStorage.setItem("pdfjs.signature", JSON.stringify(result));
  } catch (error) {
    console.error(error.message);
  }
}

// function for updating the remote page
function update_remote_page(pdf_id, update_url, csrf_token) {
  if (PDFViewerApplication.pdfViewer.currentPageNumber != page_number) {
    page_number = PDFViewerApplication.pdfViewer.currentPageNumber;
    set_current_page(page_number, pdf_id, update_url, csrf_token);
  }
}

// function for setting the current page
function set_current_page(current_page, pdf_id, update_url, csrf_token) {
  var form_data = new FormData();
  form_data.append('pdf_id', pdf_id);
  form_data.append('current_page', current_page);

  fetch(update_url, {
    method: "POST",
    body: form_data,
    headers: {
      'X-CSRFToken': csrf_token,
    },
  });
}

// function for updating the remote signatures
async function update_remote_signatures(signature_url, csrf_token) {
  const previous_signatures = localStorage.getItem("previous_pdfjs.signature");
  const current_signatures = localStorage.getItem("pdfjs.signature");

  // check if signatures were updated by pdfjs
  if (current_signatures != previous_signatures) {
    const status_code = await set_remote_signatures(current_signatures, previous_signatures, signature_url, csrf_token);

    if (status_code === 201) {
      // refresh signatures in local storage
      get_remote_signatures(signature_url);
    }
  }
}

// function for setting signatures
async function set_remote_signatures(current_signatures, previous_signatures, signature_url, csrf_token) {
  var form_data = new FormData();
  form_data.append('current_signatures', current_signatures);
  form_data.append('previous_signatures', previous_signatures);

  const response = await fetch(signature_url, {
    method: "POST",
    body: form_data,
    headers: {
      'X-CSRFToken': csrf_token,
    },
  });

  return response.status
}

// send file via the fetch api to the backend. Returns the response so callers
// can inspect the status code and react accordingly (e.g. autosave status).
async function send_pdf_file(file, pdf_id, update_url, csrf_token) {
  var form_data = new FormData();
  form_data.append('updated_pdf', file);
  form_data.append('pdf_id', pdf_id);

  return await fetch(update_url, {
    method: "POST",
    body: form_data,
    headers: {
      'X-CSRFToken': csrf_token,
    },
  });
}

// Autosave state
let autosaveTimer = null;
let autosaveDirty = false;
let autosaveStatusTimer = null;
const AUTOSAVE_DELAY_MS = 5000;

// Update the autosave status pill in the toolbar.
function set_autosave_status(state) {
  const el = document.getElementById('autosaveStatus');
  if (!el) {
    return;
  }

  if (autosaveStatusTimer) {
    clearTimeout(autosaveStatusTimer);
    autosaveStatusTimer = null;
  }

  el.classList.remove('saving', 'saved', 'error');

  if (state === 'saving') {
    el.textContent = 'Saving…';
    el.classList.add('saving');
  } else if (state === 'saved') {
    el.textContent = 'Saved';
    el.classList.add('saved');
    // fade out after 2s
    autosaveStatusTimer = setTimeout(() => {
      el.classList.remove('saved');
      el.textContent = '';
    }, 2000);
  } else if (state === 'error') {
    el.textContent = 'Save failed';
    el.classList.add('error');
  } else {
    el.textContent = '';
  }
}

// function for updating the pdf file in the backend
async function update_pdf(pdf_id, update_url, csrf_token, tab_title) {
  if (PDFViewerApplication._saveInProgress) {
    return;
  }
  PDFViewerApplication._saveInProgress = true;
  set_autosave_status('saving');
  await PDFViewerApplication.pdfScriptingManager.dispatchWillSave();

  let succeeded = false;
  try {
    const data = await PDFViewerApplication.pdfDocument.saveDocument();
    const updated_pdf = new Blob([data], {type: "application/pdf"});
    const response = await send_pdf_file(updated_pdf, pdf_id, update_url, csrf_token);

    if (response && response.ok) {
      succeeded = true;
      PDFViewerApplication._hasAnnotationEditors = false;
      // removes "*" from the tab title in order to signal that the file was successfully saved
      PDFViewerApplication.setTitle(tab_title);
      autosaveDirty = false;
      set_autosave_status('saved');
    } else {
      set_autosave_status('error');
      console.error(`Error when saving the document: HTTP ${response && response.status}`);
    }
  } catch (reason) {
    set_autosave_status('error');
    console.error(`Error when saving the document: ${reason.message}`);
  } finally {
    await PDFViewerApplication.pdfScriptingManager.dispatchDidSave();
    PDFViewerApplication._saveInProgress = false;
  }

  // If more edits came in while saving, re-schedule another autosave.
  if (succeeded && autosaveDirty) {
    schedule_autosave(pdf_id, update_url, csrf_token, tab_title);
  }
}

// Debounced autosave scheduler: each call resets a 5s timer.
function schedule_autosave(pdf_id, update_url, csrf_token, tab_title) {
  if (autosaveTimer) {
    clearTimeout(autosaveTimer);
  }
  autosaveTimer = setTimeout(() => {
    autosaveTimer = null;
    // If a manual save is currently running, wait for the next edit event to
    // re-arm. autosaveDirty will still be true.
    if (PDFViewerApplication._saveInProgress) {
      return;
    }
    update_pdf(pdf_id, update_url, csrf_token, tab_title);
  }, AUTOSAVE_DELAY_MS);
}

// Wire up autosave to pdfjs annotation editor events and page unload.
function init_autosave(pdf_id, update_url, csrf_token, tab_title) {
  const mark_dirty_and_schedule = () => {
    autosaveDirty = true;
    schedule_autosave(pdf_id, update_url, csrf_token, tab_title);
  };

  try {
    PDFViewerApplication.eventBus.on('annotationeditorstateschanged', mark_dirty_and_schedule);
  } catch (e) {
    console.error(`Could not register annotationeditorstateschanged listener: ${e.message}`);
  }

  // best-effort flush on unload: if there are unsaved edits, try to send the
  // latest PDF via sendBeacon. If saveDocument hasn't produced a blob yet we
  // simply skip to avoid blocking the unload.
  window.addEventListener('beforeunload', () => {
    if (!autosaveDirty || !PDFViewerApplication._hasAnnotationEditors) {
      return;
    }
    try {
      // Only attempt if a save isn't already in progress; saveDocument is async
      // and cannot reliably complete during unload, so we do a best-effort fire.
      if (PDFViewerApplication._saveInProgress) {
        return;
      }
      PDFViewerApplication.pdfDocument.saveDocument().then((data) => {
        const blob = new Blob([data], {type: 'application/pdf'});
        const form_data = new FormData();
        form_data.append('updated_pdf', blob);
        form_data.append('pdf_id', pdf_id);
        form_data.append('csrfmiddlewaretoken', csrf_token);
        navigator.sendBeacon(update_url, form_data);
      }).catch(() => { /* best-effort, ignore */ });
    } catch (e) {
      // swallow: we must not block unload
    }
  });
}

// function for requesting a wake lock
const requestWakeLock = async () => {
  try {
    wakeLock = await navigator.wakeLock.request();
    wakeLock.addEventListener('release', () => {
      console.log('Screen Wake Lock released:', wakeLock.released);
    });
  } catch (err) {
    console.error(`${err.name}, ${err.message}`);
  }
};
