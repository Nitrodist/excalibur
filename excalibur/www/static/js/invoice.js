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
  $('#invoice-file').on('change', function () {
    var file = this.files && this.files[0];
    if (file) {
      $(this).next('.uploadFile__label').text(file.name);
    }
  });

  $('#download-json').on('click', function () {
    var payload = document.getElementById('invoice-json');
    if (!payload) {
      return;
    }
    downloadText($(this).data('filename') + '.json', payload.textContent, 'application/json');
  });

  $('#download-csv').on('click', function () {
    var payload = document.getElementById('invoice-csv');
    if (!payload) {
      return;
    }
    downloadText($(this).data('filename') + '.csv', payload.textContent, 'text/csv');
  });
});
