from Bio import bgzf

class BlockZip():

    def __init__(self, path, mode='r'):
        self.path = path
        self.index_path = f'{path}.idx'
        self.prev_contig = None
        self.mode = mode
        self.index = {}

        if self.mode == 'w':
            self.bgzf_handle = bgzf.BgzfWriter(self.path,'w')
            self.index_handle = open(self.index_path,'wt')
        elif self.mode=='r':
            self.bgzf_handle = bgzf.BgzfReader(self.path,'rt')
            self.index_handle = open(self.index_path,'rt')

            for line in self.index_handle:
                contig, start = line.strip().split()
                self.index[contig] = int(start)
        else:
            raise ValueError('Mode can be r or w')
        self.cache = {}

    def __enter__(self):
        return self

    def __exit__(self ,type, value, traceback):
        self.bgzf_handle.close()
        self.index_handle.close()

    def write(self, contig, position, strand, data):
        assert(self.mode=='w')
        if self.prev_contig is None or self.prev_contig!=contig:
            self.index_handle.write(f'{contig}\t{int(self.bgzf_handle.tell())}\n')

        self.bgzf_handle.write(f'{contig}\t{position}\t{"+-"[strand]}\t{data}\n')
        self.prev_contig =  contig

    def __iter__(self):
        assert(self.mode=='r')
        yield from iter(self.bgzf_handle)


    def read_file_line(self, line):
        line_contig, line_pos, line_strand, rest = line.strip().split(None,3)
        return line_contig, int(line_pos), line_strand=='-', rest

    def __getitem__(self,contig_position_strand ):
        contig, position, strand = contig_position_strand
        if not contig in self.cache and contig in self.index:
            self.read_contig_to_cache(contig)
        if contig in self.cache:
            return self.cache[contig].get((position, strand), None)

    def read_contig_to_cache(self, contig):
        if not contig in self.index:
            return

        self.cache[contig] = {}
        # Seek to the start:
        self.bgzf_handle.seek(self.index[contig])

        while True:
            try:
                line = self.bgzf_handle.readline()
                if len(line)==0:
                    break
                line_contig, line_pos, line_strand, rest = self.read_file_line( line )
                #print((line_pos, line_strand,rest))
                self.cache[contig][(line_pos, line_strand)] = rest

            except Exception as e:
                raise

            if line_contig != contig:
                break
