
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

const getParams = () => {
    const innerCellRes = Number(document.querySelector('#innerCellDivisions').value)
    return { innerCellRes }
}

const draw = () => {
    const {innerCellRes } = getParams()
    const NumCells = innerCellRes;
    const Canvas = document.querySelector('canvas')
    const ctx = Canvas.getContext('2d')

    const CanvasWidth = Canvas.width;
    const CanvasHeight = Canvas.height;

    ctx.clearRect(0, 0, CanvasWidth, CanvasHeight);

    let OuterCellWidth = CanvasWidth / NumCells;
    let InnerCellWidth = OuterCellWidth / NumCells;
    let NormalInc = 1 / (NumCells - 1);

    let minR = 999
    let minG = 999
    let minB = 999
    let minA = 999

    let maxR = 0
    let maxG = 0
    let maxB = 0
    let maxA = 0
    

    for (let i = 0; i < NumCells; i++) {
        const x = OuterCellWidth * i;
        const r = NormalInc * i * 255;

        for (let j = 0; j < NumCells; j++) {
            const y = OuterCellWidth * j;
            const g = NormalInc * 255 * j

            for (let k = 0; k < NumCells; k++) {
                let b = NormalInc * 255 * k;

                let bx = k * InnerCellWidth

                for (let l = 0; l < NumCells; l++) {
                    let a = 1 / (NumCells - 1) * l;
                    
                    // Blender has this very annoying habit of discarding all colour information if the alpha is 0,
                    // so clamping this to the minimum value
                    a = Math.max(a, 1 / 255)
                    let ay = l * InnerCellWidth;
                    
                    ctx.fillStyle = `rgba(${r}, ${g}, ${b}, ${a})`
                
                    ctx.fillRect(x + bx, y + ay, InnerCellWidth, InnerCellWidth)

                    minR = Math.min(r, minR)
                    minG = Math.min(g, minG)
                    minB = Math.min(b, minB)
                    minA = Math.min(a, minA)

                    maxR = Math.max(r, maxR)
                    maxG = Math.max(g, maxG)
                    maxB = Math.max(b, maxB)
                    maxA = Math.max(a, maxA)
                }
            }
        }
    }
}

function getBase64Image(canvas) {
    var dataURL = canvas.toDataURL("image/png");
    return dataURL;
}

function downloadURI(uri, name) {
    const link = document.createElement('a');
    link.download = name;
    link.href = uri;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

window.onload = function () {
    
    draw()

    document.querySelector('#innerCellDivisions').onchange = function () {
        draw()
    }

    document.querySelector('#downloadBtn').onclick = function () {
        var base64Image = getBase64Image(document.querySelector('canvas'))
        const filename = document.querySelector('#filename').value
        downloadURI(base64Image, filename + '.png');
    };
}