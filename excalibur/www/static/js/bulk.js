function downloadText(filename, text, type) {
  var blob = new Blob([text], {type: type});
  var url = URL.createObjectURL(blob);
  var link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function pdfsFrom(fileList) {
  var pdfs = [];
  if (!fileList) {
    return pdfs;
  }
  for (var i = 0; i < fileList.length; i++) {
    var file = fileList[i];
    if (file.type === 'application/pdf' || /\.pdf$/i.test(file.name)) {
      pdfs.push(file);
    }
  }
  return pdfs;
}

function assignBulkFiles(files) {
  var input = document.getElementById('bulk-files');
  if (!input || !files.length || typeof DataTransfer === 'undefined') {
    return;
  }
  var transfer = new DataTransfer();
  for (var i = 0; i < files.length; i++) {
    transfer.items.add(files[i]);
  }
  input.files = transfer.files;
  $(input).trigger('change');
}

$(document).ready(function () {
  var $zone = $('.uploadFile');
  var dragDepth = 0;

  $('#bulk-files').on('change', function () {
    var files = this.files;
    var label = 'Upload or drop PDFs';
    if (files && files.length === 1) {
      label = files[0].name;
    } else if (files && files.length > 1) {
      label = files.length + ' PDFs selected';
    }
    $(this).next('.uploadFile__label').text(label);
  });

  $(document).on('dragenter', function (event) {
    event.preventDefault();
    dragDepth += 1;
    $zone.addClass('is-dragover');
  });
  $(document).on('dragleave', function (event) {
    event.preventDefault();
    dragDepth -= 1;
    if (dragDepth <= 0) {
      dragDepth = 0;
      $zone.removeClass('is-dragover');
    }
  });
  $(document).on('dragover', function (event) {
    event.preventDefault();
    if (event.originalEvent.dataTransfer) {
      event.originalEvent.dataTransfer.dropEffect = 'copy';
    }
  });
  $(document).on('drop', function (event) {
    event.preventDefault();
    dragDepth = 0;
    $zone.removeClass('is-dragover');
    var dropped = event.originalEvent.dataTransfer && event.originalEvent.dataTransfer.files;
    assignBulkFiles(pdfsFrom(dropped));
  });

  $('#download-bulk-csv').on('click', function () {
    var payload = document.getElementById('bulk-csv');
    if (!payload) {
      return;
    }
    downloadText('invoices.csv', payload.textContent, 'text/csv');
  });
});
