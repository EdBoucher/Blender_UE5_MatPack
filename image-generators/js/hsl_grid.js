
// H,S and L are in the 0 to 1 range

function hslToRgb(h, s, l) {
    var r, g, b;

    if (s == 0) {
        r = g = b = l; // achromatic
    } else {
        function hue2rgb(p, q, t) {
            if (t < 0) t += 1;
            if (t > 1) t -= 1;
            if (t < 1 / 6) return p + (q - p) * 6 * t;
            if (t < 1 / 2) return q;
            if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
            return p;
        }

        var q = l < 0.5 ? l * (1 + s) : l + s - l * s;
        var p = 2 * l - q;

        r = hue2rgb(p, q, h + 1 / 3);
        g = hue2rgb(p, q, h);
        b = hue2rgb(p, q, h - 1 / 3);
    }

    return [r * 255, g * 255, b * 255];
}

window.onload = function () {
    const NumCells = 32;
    const Canvas = document.querySelector('canvas')
    const ctx = Canvas.getContext('2d')

    const CanvasWidth = Canvas.width;
    const CellWidth = CanvasWidth / NumCells;

    for (let i = 0; i < NumCells; i++) {
        const x = CellWidth * i;
        const y = 0;
        const c = (255 / (NumCells - 1)) * i;
        ctx.fillStyle = `rgb(${c}, ${c}, ${c})`
        ctx.fillRect(x, y, CellWidth, CellWidth);
        console.log(x, ctx.fillStyle)

    }

    for (let i = 0; i < NumCells; i++) {
        for (let j = 1; j < NumCells; j++) {
            const x = CellWidth * i;
            const y = CellWidth * j;

            const H = (1 / (NumCells)) * i;
            const L = (1 / (NumCells + 1)) * (j + 1);

            const [r, g, b] = hslToRgb(H, 0.7, L);

            ctx.fillStyle = `rgb(${r}, ${g}, ${b})`
            ctx.fillRect(x, y, CellWidth, CellWidth);

        }
    }

     function getBase64Image(canvas) {
        var dataURL = canvas.toDataURL("image/png");
        return dataURL;
    }

    function downloadURI(uri, name) {
        // IE10+ : (has Blob, but not a[download] or URL)

        const link = document.createElement('a');
        link.download = name;
        link.href = uri;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    }

    function dataURItoBlob(dataurl) {
        const parts = dataurl.split(','), mime = parts[0].match(/:(.*?);/)[1];
        if (parts[0].indexOf('base64') !== -1) {
            const bstr = atob(parts[1]);
            let n = bstr.length;
            const u8arr = new Uint8Array(n);

            while (n--) {
                u8arr[n] = bstr.charCodeAt(n)
            }
            return new Blob([u8arr], { type: mime })
        } else {
            const raw = decodeURIComponent(parts[1])
            return new Blob([raw], { type: mime })
        }
    }

    document.querySelector('button').onclick = function () {
        console.log('click')
        var base64Image = getBase64Image(Canvas);
        downloadURI(base64Image, 'image.png');
    };
}


