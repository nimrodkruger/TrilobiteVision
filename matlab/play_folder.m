rec = tv_read_recording("D:\trilobite-data\session_20261011_203347\rec_20261011_203509_raw16_15fps");
for k = 1:(rec.frames_stored/2)
    [img_l, meta_l] = tv_recording_frame(rec, 'left', k);
    [img_r, meta_r] = tv_recording_frame(rec, 'right', k);
    subplot(1,2,1)
    imagesc(img_l); axis image; colormap gray; title('left - t = ')
       subplot(1,2,2)
    imagesc(img_r); axis image; colormap gray; title('rigth - t = ')
    pause(0.1)
end

 