#include "alltheheader.h"
struct stat statbuf;
char* buffer = "ABCDEFG\n";
int 
main(){
    mode_t mask = umask(S_IRWXU);

}