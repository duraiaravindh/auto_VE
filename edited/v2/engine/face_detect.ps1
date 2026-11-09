# Prints one JSON line per PNG in a folder, using the face detector built into Windows.
param($dir)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[void][Windows.Media.FaceAnalysis.FaceDetector,Windows.Media.FaceAnalysis,ContentType=WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
[void][Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$m=[System.WindowsRuntimeSystemExtensions].GetMethods()|?{$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'}|select -First 1
function W($op,$t){$task=$m.MakeGenericMethod($t).Invoke($null,@($op));$task.Wait();$task.Result}
$det=W ([Windows.Media.FaceAnalysis.FaceDetector]::CreateAsync()) ([Windows.Media.FaceAnalysis.FaceDetector])
Get-ChildItem $dir -Filter *.png|Sort-Object Name|%{
 $f=W ([Windows.Storage.StorageFile]::GetFileFromPathAsync($_.FullName)) ([Windows.Storage.StorageFile])
 $s=W ($f.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
 $d=W ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($s)) ([Windows.Graphics.Imaging.BitmapDecoder])
 $b=W ($d.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
 $g=[Windows.Graphics.Imaging.SoftwareBitmap]::Convert($b,[Windows.Graphics.Imaging.BitmapPixelFormat]::Gray8)
 $r=W ($det.DetectFacesAsync($g)) ([System.Collections.Generic.IList[Windows.Media.FaceAnalysis.DetectedFace]])
 $boxes=@($r|%{"[$($_.FaceBox.X),$($_.FaceBox.Y),$($_.FaceBox.Width),$($_.FaceBox.Height)]"}) -join ','
 "{`"file`":`"$($_.Name)`",`"faces`":[$boxes]}"
 $s.Dispose()
}
