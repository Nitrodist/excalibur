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

$(document).ready(function () {
  $('#bulk-files').on('change', function () {
    var files = this.files;
    var label = 'Upload PDFs';
    if (files && files.length === 1) {
      label = files[0].name;
    } else if (files && files.length > 1) {
      label = files.length + ' PDFs selected';
    }
    $(this).next('.uploadFile__label').text(label);
  });

  $('#download-bulk-csv').on('click', function () {
    var payload = document.getElementById('bulk-csv');
    if (!payload) {
      return;
    }
    downloadText('invoices.csv', payload.textContent, 'text/csv');
  });
});
